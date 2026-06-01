"""/groups/{group_id}/feed router.

Thin handler — cursor logic and post hydration live in services/feed.py.
Mounted under the /groups prefix; the path is deeper than the groups
router's /{group_id}, so there's no route-shadowing between them.

Error mapping:
- GroupNotFoundError → 404 (caller not a member, or no such group)
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from supabase import Client

from app.auth.jwt import get_current_user_id
from app.clients.supabase import get_supabase
from app.models.post import FeedPage
from app.services import feed as feed_service

router = APIRouter(prefix="/groups", tags=["feed"])


@router.get("/{group_id}/feed", response_model=FeedPage)
def get_feed(
    group_id: UUID,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
    user_id: UUID = Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
) -> FeedPage:
    try:
        return feed_service.list_feed(sb, user_id, group_id, cursor=cursor, limit=limit)
    except feed_service.GroupNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        ) from e
