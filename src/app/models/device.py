"""Pydantic models for FCM device registration.

Mirrors `public.devices` in supabase/migrations/20260529212325_create_mvp_tables.sql.
A device is keyed by its FCM token (unique); ownership can transfer between
users when a shared device re-logs in (last login wins).
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


class DevicePlatform(StrEnum):
    IOS = "ios"
    ANDROID = "android"


class DeviceRegisterRequest(BaseModel):
    # FCM tokens are ~163 chars in practice but the spec allows up to ~4 KB.
    fcm_token: str = Field(min_length=1, max_length=4096)
    platform: DevicePlatform


class Device(BaseModel):
    id: UUID
    user_id: UUID
    fcm_token: str
    platform: DevicePlatform
    last_seen_at: datetime
    created_at: datetime
