from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, computed_field

from app.config import RETENTION_DAYS

Lifecycle = Literal["upcoming", "active", "locked"]


class GroupSettings(BaseModel):
    """Per-group runtime settings — defaults from /docs/02-product-design.md §2.5.

    Values here mirror the DB defaults on the groups table. Any field the
    caller leaves unset is dropped via `model_dump(exclude_unset=True)` so
    the DB-side default still wins (defence in depth).
    """

    prompts_per_day: int = Field(default=4, gt=0)
    daily_window_start: time = time(10, 0)
    daily_window_end: time = time(1, 0)
    min_prompt_gap_minutes: int = Field(default=45, ge=0)
    response_window_seconds: int = Field(default=300, gt=0)
    late_window_seconds: int = Field(default=1800, ge=0)
    max_video_length_seconds: int = Field(default=10, gt=0)


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    start_date: date
    end_date: date
    settings: GroupSettings | None = None


class Group(BaseModel):
    id: UUID
    name: str
    created_by: UUID
    start_date: date
    end_date: date
    prompts_per_day: int
    daily_window_start: time
    daily_window_end: time
    min_prompt_gap_minutes: int
    response_window_seconds: int
    late_window_seconds: int
    max_video_length_seconds: int
    view_delay_seconds: int
    created_at: datetime
    updated_at: datetime


class GroupWithLifecycle(Group):
    lifecycle: Lifecycle

    @computed_field  # type: ignore[prop-decorator]
    @property
    def retention_purge_at(self) -> datetime:
        """When this group's media becomes eligible for purge: end_date +
        RETENTION_DAYS, at UTC midnight. Computed on read (no DB column) and
        used purely client-side to drive the pre-expiry save nudge. As a
        computed field it rides along on every construction path
        (groups + members services) without per-call wiring.
        """
        return datetime.combine(
            self.end_date + timedelta(days=RETENTION_DAYS), time.min, tzinfo=UTC
        )


class GroupCreateResponse(BaseModel):
    group: GroupWithLifecycle
    invite_code: str


class GroupMember(BaseModel):
    user_id: UUID
    display_name: str | None
    avatar_url: str | None
    joined_at: datetime


class JoinGroupRequest(BaseModel):
    code: str = Field(min_length=6, max_length=16)
