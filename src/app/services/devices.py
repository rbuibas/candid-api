"""Devices service — register / delete FCM tokens.

A device row is keyed by its FCM token. Registration upserts on the unique
token, which intentionally transfers ownership to whoever logs in last — the
correct behavior for a shared phone or a re-install.

Deletion is caller-scoped: callers can only delete their own tokens. Unknown
or other-user tokens return DeviceNotFoundError (404 — same anti-leak shape
as groups).
"""

from datetime import UTC, datetime
from uuid import UUID

from supabase import Client

from app.models.device import Device, DeviceRegisterRequest


class DeviceNotFoundError(Exception):
    """Either the token doesn't exist or it belongs to a different user."""


def register(sb: Client, user_id: UUID, payload: DeviceRegisterRequest) -> Device:
    row = {
        "user_id": str(user_id),
        "fcm_token": payload.fcm_token,
        "platform": payload.platform.value,
        "last_seen_at": datetime.now(UTC).isoformat(),
    }
    result = sb.table("devices").upsert(row, on_conflict="fcm_token").execute()
    if not result.data:
        raise RuntimeError("devices upsert returned no row")
    return Device.model_validate(result.data[0])


def delete_for_user(sb: Client, user_id: UUID, fcm_token: str) -> None:
    existing = (
        sb.table("devices").select("user_id").eq("fcm_token", fcm_token).maybe_single().execute()
    )
    if not existing or not existing.data:
        raise DeviceNotFoundError()
    if existing.data["user_id"] != str(user_id):
        raise DeviceNotFoundError()
    sb.table("devices").delete().eq("fcm_token", fcm_token).execute()
