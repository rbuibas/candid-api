"""Profile service — DB access for profile read/update, plus avatar handling.

Router handlers stay thin: they call into here, catch the domain exceptions,
and translate them to HTTP responses.

### Avatar storage

Decision: `profiles.avatar_url` stores the R2 storage_path (e.g.
``users/<user_id>/avatars/<uuid>.jpg``), and **every read path resolves it
to a freshly-minted ~1h signed GET URL** before returning the model. This
mirrors the post-read pattern (no stale-URL refresh logic on the client)
and keeps the wire contract for `avatar_url` unchanged (still an https URL).

The two write paths are split per /docs/03-technical-architecture.md §3:

1. ``POST /profile/avatar/upload-url`` mints a presigned PUT for a brand-new
   R2 key under ``users/<user_id>/avatars/<uuid>.<ext>``.
2. ``PATCH /profile/avatar`` HeadObjects the storage_path the client sends
   back, verifies it lives under the caller's avatar prefix, and writes the
   key into ``profiles.avatar_url``. Old avatar objects are left in the
   bucket for now — overwrites use a fresh uuid so signed URLs already in
   flight don't break.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from supabase import Client

from app.clients import r2
from app.models.post import AvatarUploadUrlResponse
from app.models.profile import Profile, ProfileUpdate

_UPLOAD_TTL_SECONDS = 600
_DOWNLOAD_TTL_SECONDS = 3600


class ProfileNotFoundError(Exception):
    """The authenticated user has no profiles row.

    Should be unreachable in normal flow because the handle_new_user
    trigger creates the row on auth.users insert. Treated as 401 by
    callers (token is valid but the user doesn't exist in our world).
    """


class EmptyPatchError(Exception):
    """Caller sent a PATCH body with no fields set."""


class AvatarPathMismatchError(Exception):
    """storage_path is not under the caller's avatar prefix."""


class AvatarObjectMissingError(Exception):
    """HeadObject failed at PATCH — the client never PUT the bytes."""


def resolve_avatar_url(storage_path: str | None) -> str | None:
    """Convert a stored R2 key to a fresh ~1h signed GET URL.

    Returns None if the stored value is empty. Pass-through if the stored
    value is already an absolute URL (legacy / test data).
    """
    if not storage_path:
        return None
    if storage_path.startswith("http://") or storage_path.startswith("https://"):
        return storage_path
    return r2.generate_presigned_get_url(storage_path, ttl_seconds=_DOWNLOAD_TTL_SECONDS)


def _profile_with_signed_avatar(row: dict) -> Profile:
    profile = Profile.model_validate(row)
    if profile.avatar_url is not None:
        profile = profile.model_copy(update={"avatar_url": resolve_avatar_url(profile.avatar_url)})
    return profile


def get_for_user(sb: Client, user_id: UUID) -> Profile:
    result = sb.table("profiles").select("*").eq("id", str(user_id)).maybe_single().execute()
    if not result.data:
        raise ProfileNotFoundError()
    return _profile_with_signed_avatar(result.data)


def update_for_user(sb: Client, user_id: UUID, patch: ProfileUpdate) -> Profile:
    update_dict = patch.model_dump(exclude_unset=True)
    if not update_dict:
        raise EmptyPatchError()

    result = sb.table("profiles").update(update_dict).eq("id", str(user_id)).execute()
    if not result.data:
        raise ProfileNotFoundError()
    return _profile_with_signed_avatar(result.data[0])


def _avatar_prefix(user_id: UUID) -> str:
    return f"users/{user_id}/avatars/"


def create_avatar_upload_url(user_id: UUID, extension: str) -> AvatarUploadUrlResponse:
    storage_path = f"{_avatar_prefix(user_id)}{uuid4()}.{extension.lower()}"
    upload_url = r2.generate_presigned_put_url(
        storage_path, "image/jpeg", ttl_seconds=_UPLOAD_TTL_SECONDS
    )
    expires_at = datetime.now(UTC) + timedelta(seconds=_UPLOAD_TTL_SECONDS)
    return AvatarUploadUrlResponse(
        upload_url=upload_url, storage_path=storage_path, expires_at=expires_at
    )


def set_avatar(sb: Client, user_id: UUID, storage_path: str) -> Profile:
    if not storage_path.startswith(_avatar_prefix(user_id)):
        raise AvatarPathMismatchError()
    if not r2.head_object(storage_path):
        raise AvatarObjectMissingError()

    result = (
        sb.table("profiles").update({"avatar_url": storage_path}).eq("id", str(user_id)).execute()
    )
    if not result.data:
        raise ProfileNotFoundError()
    return _profile_with_signed_avatar(result.data[0])
