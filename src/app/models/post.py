"""Pydantic models for the Phase 3 capture & media pipeline.

Mirrors the column layout in supabase/migrations/20260529212325_create_mvp_tables.sql.
The `accuracy` field in ConfirmPostRequest maps onto the DB column
`location_accuracy_meters` — the service rounds the float into an int.
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field

# Allowed file extensions for upload-url requests. Kept tiny on purpose:
# alphanumeric only, 1–8 chars. Server still lowercases before composing
# the storage_path.
_EXTENSION_PATTERN = r"^[A-Za-z0-9]+$"


class PostKind(StrEnum):
    PROMPT = "prompt"
    PHOTOBOOTH = "photobooth"


class PostMediaType(StrEnum):
    PHOTO = "photo"
    VIDEO = "video"
    STRIP = "strip"


class UploadUrlRequest(BaseModel):
    group_id: UUID
    kind: PostKind
    media_type: PostMediaType
    prompt_id: UUID | None = None
    extension: str = Field(min_length=1, max_length=8, pattern=_EXTENSION_PATTERN)


class UploadUrlResponse(BaseModel):
    post_id: UUID
    upload_url: str
    storage_path: str
    expires_at: datetime


class ConfirmPostRequest(BaseModel):
    post_id: UUID
    group_id: UUID
    kind: PostKind
    media_type: PostMediaType
    storage_path: str
    captured_at: datetime
    duration_seconds: int | None = Field(default=None, ge=0)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0)
    prompt_id: UUID | None = None


class Post(BaseModel):
    id: UUID
    prompt_id: UUID | None
    group_id: UUID
    user_id: UUID
    kind: PostKind
    media_type: PostMediaType
    storage_path: str
    thumbnail_path: str | None
    duration_seconds: int | None
    captured_at: datetime
    is_late: bool
    visible_at: datetime
    latitude: float | None
    longitude: float | None
    location_accuracy_meters: int | None
    created_at: datetime


class PostWithMediaUrl(Post):
    media_url: str


class AvatarUploadUrlRequest(BaseModel):
    extension: str = Field(min_length=1, max_length=8, pattern=_EXTENSION_PATTERN)


class AvatarUploadUrlResponse(BaseModel):
    upload_url: str
    storage_path: str
    expires_at: datetime


class AvatarPatch(BaseModel):
    storage_path: str
