"""Posts service — upload-url / confirm / get.

The flow per /docs/03-technical-architecture.md §3:

1. Client → POST /posts/upload-url. We mint a post_id, compose the R2 key,
   and hand back a presigned PUT URL. **No** DB row inserted here — the
   row only exists after confirm.
2. Client PUTs media bytes straight to R2.
3. Client → POST /posts/confirm. We HeadObject to verify the upload landed,
   compute visible_at = now + group.view_delay_seconds, and insert the row.
   Idempotent on post_id: a re-call from the same user for the same
   (post_id, group_id) returns the existing row at 200.

Service-role bypasses RLS at this layer, so we manually scope every read
by membership. RLS is still the second line of defence for the rare case
this layer is reached with a different key.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from postgrest.exceptions import APIError
from supabase import Client

from app.clients import r2
from app.models.post import (
    ConfirmPostRequest,
    Post,
    PostMediaType,
    PostWithMediaUrl,
    UploadUrlRequest,
    UploadUrlResponse,
)

_PG_UNIQUE_VIOLATION = "23505"
_UPLOAD_TTL_SECONDS = 600
_DOWNLOAD_TTL_SECONDS = 3600


class GroupNotFoundError(Exception):
    """Caller is not a member of the requested group (or group doesn't exist).

    Routers map this to 404 — same anti-leak convention used by groups.
    """


class PostNotFoundError(Exception):
    """GET /posts/{id} and the post does not exist (or is tombstoned)."""


class PostNotAccessibleError(Exception):
    """Post exists but the caller cannot see / confirm it.

    Routers map this to 403. Knowingly differs from the groups 404-anti-leak
    convention: a post id is an opaque UUID, so the leak from 403 vs 404 is
    negligible, and 403 communicates intent better.
    """


class MediaObjectMissingError(Exception):
    """HeadObject failed at confirm — the client never PUT the bytes."""


class StoragePathMismatchError(Exception):
    """storage_path doesn't match the shape the server would have minted.

    Defense-in-depth against a forged confirm body that points HeadObject at
    a different object the caller happens to have access to.
    """


def _post_storage_path(group_id: UUID, post_id: UUID, extension: str) -> str:
    return f"groups/{group_id}/posts/{post_id}/media.{extension.lower()}"


def _post_storage_path_prefix(group_id: UUID, post_id: UUID) -> str:
    return f"groups/{group_id}/posts/{post_id}/media."


def _content_type_for(media_type: PostMediaType) -> str:
    # Strip is a client-composed JPEG; photo is also JPEG. Video is mp4
    # (vision-camera default container on both platforms).
    return "video/mp4" if media_type is PostMediaType.VIDEO else "image/jpeg"


def _is_member(sb: Client, user_id: UUID, group_id: UUID) -> bool:
    result = (
        sb.table("group_members")
        .select("id")
        .eq("group_id", str(group_id))
        .eq("user_id", str(user_id))
        .maybe_single()
        .execute()
    )
    return bool(result and result.data)


def _select_post(sb: Client, post_id: UUID) -> dict | None:
    result = (
        sb.table("posts")
        .select("*")
        .eq("id", str(post_id))
        .is_("deleted_at", "null")
        .maybe_single()
        .execute()
    )
    if not result or not result.data:
        return None
    return result.data


def create_upload_url(
    sb: Client,
    user_id: UUID,
    payload: UploadUrlRequest,
) -> UploadUrlResponse:
    if not _is_member(sb, user_id, payload.group_id):
        raise GroupNotFoundError()

    post_id = uuid4()
    storage_path = _post_storage_path(payload.group_id, post_id, payload.extension)
    upload_url = r2.generate_presigned_put_url(
        storage_path,
        _content_type_for(payload.media_type),
        ttl_seconds=_UPLOAD_TTL_SECONDS,
    )
    expires_at = datetime.now(UTC) + timedelta(seconds=_UPLOAD_TTL_SECONDS)
    return UploadUrlResponse(
        post_id=post_id,
        upload_url=upload_url,
        storage_path=storage_path,
        expires_at=expires_at,
    )


def confirm(sb: Client, user_id: UUID, payload: ConfirmPostRequest) -> Post:
    # 1. Idempotency: existing row for this post_id wins, provided the caller
    #    owns it and the group matches. Mismatches → 403 (tampered re-call).
    existing = _select_post(sb, payload.post_id)
    if existing is not None:
        if existing["user_id"] != str(user_id) or existing["group_id"] != str(payload.group_id):
            raise PostNotAccessibleError()
        return Post.model_validate(existing)

    # 2. Membership check (also covers "no such group").
    if not _is_member(sb, user_id, payload.group_id):
        raise GroupNotFoundError()

    # 3. Storage-path tamper check.
    expected_prefix = _post_storage_path_prefix(payload.group_id, payload.post_id)
    if not payload.storage_path.startswith(expected_prefix):
        raise StoragePathMismatchError()

    # 4. Object must exist at the path.
    if not r2.head_object(payload.storage_path):
        raise MediaObjectMissingError()

    # 5. visible_at = now + group.view_delay_seconds.
    group_row = (
        sb.table("groups")
        .select("view_delay_seconds")
        .eq("id", str(payload.group_id))
        .maybe_single()
        .execute()
    )
    if not group_row or not group_row.data:
        raise GroupNotFoundError()
    view_delay_seconds = int(group_row.data["view_delay_seconds"])
    visible_at = datetime.now(UTC) + timedelta(seconds=view_delay_seconds)

    accuracy_int: int | None = None
    if payload.accuracy is not None:
        accuracy_int = int(round(payload.accuracy))

    insert_payload = {
        "id": str(payload.post_id),
        "user_id": str(user_id),
        "group_id": str(payload.group_id),
        "prompt_id": str(payload.prompt_id) if payload.prompt_id else None,
        "kind": payload.kind.value,
        "media_type": payload.media_type.value,
        "storage_path": payload.storage_path,
        "duration_seconds": payload.duration_seconds,
        "captured_at": payload.captured_at.isoformat(),
        "is_late": False,
        "visible_at": visible_at.isoformat(),
        "latitude": payload.latitude,
        "longitude": payload.longitude,
        "location_accuracy_meters": accuracy_int,
    }

    # 6. Insert. Concurrent confirms race on the posts.id PK; treat the
    #    unique_violation as the idempotent case and re-fetch.
    try:
        insert_result = sb.table("posts").insert(insert_payload).execute()
    except APIError as e:
        if e.code != _PG_UNIQUE_VIOLATION:
            raise
        racing = _select_post(sb, payload.post_id)
        if racing is None:
            raise
        return Post.model_validate(racing)

    if not insert_result.data:
        raise RuntimeError("posts insert returned no row")
    return Post.model_validate(insert_result.data[0])


def get_post(sb: Client, user_id: UUID, post_id: UUID) -> PostWithMediaUrl:
    row = _select_post(sb, post_id)
    if row is None:
        raise PostNotFoundError()
    post = Post.model_validate(row)
    if not _is_member(sb, user_id, post.group_id):
        raise PostNotAccessibleError()
    media_url = r2.generate_presigned_get_url(post.storage_path, ttl_seconds=_DOWNLOAD_TTL_SECONDS)
    return PostWithMediaUrl(**post.model_dump(), media_url=media_url)
