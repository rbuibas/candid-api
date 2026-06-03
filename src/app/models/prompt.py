"""Pydantic models for prompts.

`Prompt` mirrors `public.prompts` in
supabase/migrations/20260529212325_create_mvp_tables.sql. `PromptView` is the
read-shape returned by /prompts/active and /prompts/{id} — it folds the
group's response/late windows into pre-computed deadlines and a UI state so
the client renders countdowns without recomputing lateness.
"""

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel


class PromptMediaType(StrEnum):
    PHOTO = "photo"
    VIDEO = "video"


class PromptStatus(StrEnum):
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    RESPONDED = "responded"
    LATE = "late"
    MISSED = "missed"


class PromptUIState(StrEnum):
    # Computed from server NOW vs dispatched_at deadlines. Distinct from the
    # DB enum: a DB `status='active'` row can be UI-state `active`, `late`,
    # or `missed` depending on when the client reads it.
    ACTIVE = "active"
    LATE = "late"
    MISSED = "missed"
    # DB status='responded' or 'late' (already captured) — returned so the
    # mobile screen can show a "you've already captured this" state instead of
    # re-presenting the capture CTA.
    RESPONDED = "responded"


class Prompt(BaseModel):
    id: UUID
    group_id: UUID
    user_id: UUID
    scheduled_at: datetime
    dispatched_at: datetime | None
    local_date: date
    media_type: PromptMediaType
    target_video_length_seconds: int | None
    status: PromptStatus
    created_at: datetime


class PromptView(BaseModel):
    id: UUID
    group_id: UUID
    media_type: PromptMediaType
    target_video_length_seconds: int | None
    dispatched_at: datetime
    on_time_deadline: datetime
    late_deadline: datetime
    state: PromptUIState


class FirePromptRequest(BaseModel):
    group_id: UUID
    media_type: PromptMediaType | None = None


class TriggerPromptRequest(BaseModel):
    """Body for POST /dev/prompts/trigger — media_type is required here."""

    group_id: UUID
    media_type: PromptMediaType
