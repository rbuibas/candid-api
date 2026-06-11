"""/events router.

POST /events records one small, group-scoped client event
(candid-measurement-and-debrief §3). Thin handler — the membership check and
insert live in services/events.py.

- GroupNotFoundError → 404 (caller not in group, or no such group; anti-leak)
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client

from app.auth.jwt import get_current_user_id
from app.clients.supabase import get_supabase
from app.models.event import ClientEvent, ClientEventCreate
from app.services import events as events_service

router = APIRouter(prefix="/events", tags=["events"])


@router.post("", response_model=ClientEvent, status_code=status.HTTP_201_CREATED)
def create_event(
    payload: ClientEventCreate,
    user_id: UUID = Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
) -> ClientEvent:
    try:
        return events_service.record(sb, user_id, payload)
    except events_service.GroupNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        ) from e
