from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class Profile(BaseModel):
    id: UUID
    display_name: str | None
    avatar_url: str | None
    timezone: str
    created_at: datetime
    updated_at: datetime


class ProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=80)
    timezone: str | None = Field(default=None, max_length=64)
    avatar_url: str | None = None
