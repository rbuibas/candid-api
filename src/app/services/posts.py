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

Phase 4 extension — when kind=prompt, confirm enforces the prompt window
anchored on `dispatched_at` (server-authoritative). Receipt past
late_deadline returns 410; receipt past on-time but within late_window marks
the post is_late=true and flips the prompt to status='late'. The
posts_prompt_id_unique partial index guards against a second confirm
racing for the same prompt.

Service-role bypasses RLS at this layer, so we manually scope every read
by membership. RLS is still the second line of defence for the rare case
this layer is reached with a different key.
"""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from postgrest.exceptions import APIError
from supabase import Client

from app.clients import r2
from app.models.post import (
    ConfirmPostRequest,
    Post,
    PostKind,
    PostMediaType,
    PostWithMediaUrl,
    UploadUrlRequest,
    UploadUrlResponse,
)
from app.services.prompts import compute_deadlines

log = logging.getLogger(__name__)

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


class PromptIdRequiredError(Exception):
    """confirm body has kind=prompt but no prompt_id. Router maps to 422."""


class PromptNotAccessibleError(Exception):
    """Prompt does not exist, is owned by another user, or its group_id does
    not match the confirm payload. Router maps to 403 (anti-leak)."""


class PromptNotActiveError(Exception):
    """Prompt is not in status='active' (already responded/late/missed/scheduled),
    OR a second confirm raced and lost on the prompt_id unique index.
    Router maps to 409."""


class PromptExpiredError(Exception):
    """Server receipt time is past the late_deadline. No post is inserted;
    the expirer will mark the prompt missed on its next tick. Router maps to 410."""


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


def _load_active_prompt(
    sb: Client,
    user_id: UUID,
    group_id: UUID,
    prompt_id: UUID,
) -> dict:
    """Fetch a prompt joined with its group's window settings.

    Raises PromptNotAccessibleError if the row doesn't exist, isn't the
    caller's, or its group_id mismatches the confirm payload.
    Raises PromptNotActiveError if the row is no longer status='active' or
    dispatched_at is null.
    """
    result = (
        sb.table("prompts")
        .select("*, groups(response_window_seconds, late_window_seconds)")
        .eq("id", str(prompt_id))
        .maybe_single()
        .execute()
    )
    if not result or not result.data:
        raise PromptNotAccessibleError()
    row = result.data
    if row["user_id"] != str(user_id) or row["group_id"] != str(group_id):
        raise PromptNotAccessibleError()
    if row["status"] != "active" or row.get("dispatched_at") is None:
        raise PromptNotActiveError()
    return row


def confirm(sb: Client, user_id: UUID, payload: ConfirmPostRequest) -> Post:
    # 1. Idempotency: existing row for this post_id wins, provided the caller
    #    owns it and the group matches. Mismatches → 403 (tampered re-call).
    #    For prompt confirms we deliberately skip the post-insert status
    #    flip below — the prompt is already in its terminal state from the
    #    first call.
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

    # 5. Prompt-window enforcement (kind=prompt only). Lateness is computed
    #    from server-receipt time vs. dispatched_at deadlines — never
    #    captured_at, never client clock.
    is_late = False
    prompt_row: dict | None = None
    if payload.kind is PostKind.PROMPT:
        if payload.prompt_id is None:
            raise PromptIdRequiredError()
        prompt_row = _load_active_prompt(sb, user_id, payload.group_id, payload.prompt_id)
        dispatched_at = datetime.fromisoformat(prompt_row["dispatched_at"]).astimezone(UTC)
        rws = int(prompt_row["groups"]["response_window_seconds"])
        lws = int(prompt_row["groups"]["late_window_seconds"])
        on_time, late = compute_deadlines(dispatched_at, rws, lws)
        now = datetime.now(UTC)
        if now > late:
            raise PromptExpiredError()
        is_late = now > on_time

    # 6. visible_at = now + group.view_delay_seconds.
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
        "is_late": is_late,
        "visible_at": visible_at.isoformat(),
        "latitude": payload.latitude,
        "longitude": payload.longitude,
        "location_accuracy_meters": accuracy_int,
    }

    # 7. Insert. Two possible 23505 (unique_violation) shapes:
    #    a) same post_id raced → idempotent re-fetch
    #    b) different post_id, same prompt_id (posts_prompt_id_unique) →
    #       another confirm beat us; surface as PromptNotActiveError.
    try:
        insert_result = sb.table("posts").insert(insert_payload).execute()
    except APIError as e:
        if e.code != _PG_UNIQUE_VIOLATION:
            raise
        racing = _select_post(sb, payload.post_id)
        if racing is not None:
            return Post.model_validate(racing)
        if payload.kind is PostKind.PROMPT:
            raise PromptNotActiveError() from e
        raise

    if not insert_result.data:
        raise RuntimeError("posts insert returned no row")

    # 8. Best-effort prompt status flip. The partial unique index on
    #    posts.prompt_id guarantees a re-confirm collides on insert, so a
    #    silently-failing flip here only affects what /prompts/active
    #    returns until the next read — not correctness.
    if payload.kind is PostKind.PROMPT and payload.prompt_id is not None:
        new_status = "late" if is_late else "responded"
        try:
            sb.table("prompts").update({"status": new_status}).eq(
                "id", str(payload.prompt_id)
            ).execute()
        except Exception:
            log.exception(
                "prompt status flip failed for prompt_id=%s; post inserted",
                payload.prompt_id,
            )

    return Post.model_validate(insert_result.data[0])


def get_my_photobooth_post(sb: Client, user_id: UUID, group_id: UUID) -> PostWithMediaUrl | None:
    """Return the caller's photobooth strip post for this group, or None if not done yet."""
    result = (
        sb.table("posts")
        .select("*")
        .eq("group_id", str(group_id))
        .eq("user_id", str(user_id))
        .eq("kind", "photobooth")
        .is_("deleted_at", "null")
        .limit(1)
        .execute()
    )
    if not result.data:
        return None
    post = Post.model_validate(result.data[0])
    media_url = r2.generate_presigned_get_url(post.storage_path, ttl_seconds=_DOWNLOAD_TTL_SECONDS)
    return PostWithMediaUrl(**post.model_dump(), media_url=media_url)


def get_post(sb: Client, user_id: UUID, post_id: UUID) -> PostWithMediaUrl:
    row = _select_post(sb, post_id)
    if row is None:
        raise PostNotFoundError()
    post = Post.model_validate(row)
    if not _is_member(sb, user_id, post.group_id):
        raise PostNotAccessibleError()
    media_url = r2.generate_presigned_get_url(post.storage_path, ttl_seconds=_DOWNLOAD_TTL_SECONDS)
    return PostWithMediaUrl(**post.model_dump(), media_url=media_url)


def delete_post(sb: Client, user_id: UUID, post_id: UUID) -> None:
    """Author-only soft delete: tombstone the row, then hard-delete R2 objects.

    Idempotent on already-deleted: ``_select_post`` filters
    ``deleted_at IS NULL``, so a re-delete of a tombstoned (or never-existed)
    post raises PostNotFoundError → 404. A non-author raises
    PostNotAccessibleError → 403.

    The tombstone UPDATE is itself guarded by ``deleted_at IS NULL`` so a
    concurrent double-delete can't double-fire the R2 purge; the loser of
    that race gets 0 rows back and surfaces as 404.

    R2 deletion is best-effort: the row is already tombstoned (invisible to
    the feed), so a transient R2 error is logged, not propagated — re-running
    delete won't help since the row is gone from the visible set.
    """
    row = _select_post(sb, post_id)
    if row is None:
        raise PostNotFoundError()
    if row["user_id"] != str(user_id):
        raise PostNotAccessibleError()

    update_result = (
        sb.table("posts")
        .update({"deleted_at": datetime.now(UTC).isoformat()})
        .eq("id", str(post_id))
        .is_("deleted_at", "null")
        .execute()
    )
    if not update_result.data:
        # Lost a double-delete race — another request tombstoned it first.
        raise PostNotFoundError()

    for path in (row.get("storage_path"), row.get("thumbnail_path")):
        if not path:
            continue
        try:
            r2.delete_object(path)
        except Exception:
            log.exception("R2 delete failed for path=%s during post delete", path)
