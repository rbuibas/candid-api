from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client

from app.auth.jwt import get_current_user_id
from app.clients.supabase import get_supabase
from app.models.profile import ProfileRead, ProfileUpdate

router = APIRouter(prefix="/profile", tags=["profile"])


@router.get("", response_model=ProfileRead)
def get_profile(
    user_id: UUID = Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
) -> ProfileRead:
    result = sb.table("profiles").select("*").eq("id", str(user_id)).single().execute()
    if not result.data:
        # handle_new_user trigger creates the row at signup; this is unreachable
        # in normal flow but guards against a manual auth.users insert that skipped it.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Profile not found",
        )
    return ProfileRead.model_validate(result.data)


@router.patch("", response_model=ProfileRead)
def update_profile(
    payload: ProfileUpdate,
    user_id: UUID = Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
) -> ProfileRead:
    update_dict = payload.model_dump(exclude_unset=True)
    if not update_dict:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one field must be provided",
        )

    result = sb.table("profiles").update(update_dict).eq("id", str(user_id)).execute()
    if not result.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Profile not found",
        )
    return ProfileRead.model_validate(result.data[0])
