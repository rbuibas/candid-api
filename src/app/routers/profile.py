from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client

from app.auth.jwt import get_current_user, get_current_user_id
from app.clients.supabase import get_supabase
from app.models.post import AvatarPatch, AvatarUploadUrlRequest, AvatarUploadUrlResponse
from app.models.profile import Profile, ProfileUpdate
from app.services import profile as profile_service

router = APIRouter(prefix="/profile", tags=["profile"])


@router.get("/me", response_model=Profile)
def get_me(profile: Profile = Depends(get_current_user)) -> Profile:
    # get_current_user already fetched the profile — just return it.
    return profile


@router.patch("/me", response_model=Profile)
def patch_me(
    payload: ProfileUpdate,
    user_id: UUID = Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
) -> Profile:
    try:
        return profile_service.update_for_user(sb, user_id, payload)
    except profile_service.EmptyPatchError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one field must be provided",
        ) from e
    except profile_service.ProfileNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Profile not found",
        ) from e


@router.post("/avatar/upload-url", response_model=AvatarUploadUrlResponse)
def avatar_upload_url(
    payload: AvatarUploadUrlRequest,
    user_id: UUID = Depends(get_current_user_id),
) -> AvatarUploadUrlResponse:
    return profile_service.create_avatar_upload_url(user_id, payload.extension)


@router.patch("/avatar", response_model=Profile)
def patch_avatar(
    payload: AvatarPatch,
    user_id: UUID = Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
) -> Profile:
    try:
        return profile_service.set_avatar(sb, user_id, payload.storage_path)
    except profile_service.AvatarPathMismatchError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="storage_path is not under your avatar prefix",
        ) from e
    except profile_service.AvatarObjectMissingError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Avatar object not found at storage_path",
        ) from e
    except profile_service.ProfileNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Profile not found",
        ) from e
