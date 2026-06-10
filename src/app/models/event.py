"""Pydantic models for client events.

Mirrors `public.client_events` in
supabase/migrations/20260610120000_create_client_events.sql — a small,
group-scoped, EU-resident analytics sink (candid-measurement-and-debrief §3).

`payload` is free-form JSON: each event name defines its own shape
client-side (e.g. `feed_opened` carries `{ "source": "standalone" }`). The
server stores it verbatim, so we keep the type permissive here rather than
modelling every event's payload.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class ClientEventCreate(BaseModel):
    group_id: UUID
    name: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)


class ClientEvent(BaseModel):
    id: UUID
    group_id: UUID
    user_id: UUID
    name: str
    payload: dict[str, Any]
    created_at: datetime
