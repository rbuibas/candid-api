from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from supabase import Client

from app.auth.jwt import get_current_user_id
from app.clients.supabase import get_supabase
from app.models.group import (
    GroupCreate,
    GroupCreateResponse,
    GroupMember,
    GroupWithLifecycle,
    JoinGroupRequest,
)
from app.models.post import PostWithMediaUrl
from app.services import groups as groups_service
from app.services import members as members_service
from app.services import posts as posts_service

router = APIRouter(prefix="/groups", tags=["groups"])


@router.post("", response_model=GroupCreateResponse, status_code=status.HTTP_201_CREATED)
def create_group(
    payload: GroupCreate,
    user_id: UUID = Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
) -> GroupCreateResponse:
    return groups_service.create(sb, user_id, payload)


@router.get("", response_model=list[GroupWithLifecycle])
def list_groups(
    user_id: UUID = Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
) -> list[GroupWithLifecycle]:
    return groups_service.list_for_user(sb, user_id)


@router.post("/join", response_model=GroupWithLifecycle)
def join_group(
    payload: JoinGroupRequest,
    user_id: UUID = Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
) -> GroupWithLifecycle:
    try:
        return members_service.join(sb, user_id, payload.code)
    except members_service.InvalidInviteCodeError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invite code not found or inactive",
        ) from e


@router.get("/{group_id}/photobooth/me", response_model=PostWithMediaUrl)
def get_my_photobooth_post(
    group_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
) -> PostWithMediaUrl:
    post = posts_service.get_my_photobooth_post(sb, user_id, group_id)
    if post is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No photobooth post found for this user in this group",
        )
    return post


@router.get("/{group_id}", response_model=GroupWithLifecycle)
def get_group(
    group_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
) -> GroupWithLifecycle:
    try:
        return groups_service.get(sb, user_id, group_id)
    except groups_service.GroupNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        ) from e


@router.get("/{group_id}/members", response_model=list[GroupMember])
def list_members(
    group_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
) -> list[GroupMember]:
    try:
        return members_service.list_for_group(sb, user_id, group_id)
    except groups_service.GroupNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        ) from e


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_group(
    group_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
) -> Response:
    try:
        groups_service.delete(sb, user_id, group_id)
    except groups_service.GroupNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        ) from e
    except groups_service.NotGroupCreatorError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the creator can delete this group",
        ) from e
    return Response(status_code=status.HTTP_204_NO_CONTENT)
