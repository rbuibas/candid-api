"""/posts router.

Thin handlers — the work lives in services/posts.py. The error translation
table:

- GroupNotFoundError        → 404 (caller not in group, or no such group)
- GroupLockedError          → 409 {"error": "group_locked"} (group past end_date)
- PostNotFoundError         → 404 (no such post / tombstoned)
- PostNotAccessibleError    → 403 (post exists, caller not in its group;
                                    or tampered re-confirm)
- MediaObjectMissingError   → 422 (client never PUT the bytes)
- StoragePathMismatchError  → 422 (storage_path doesn't match the server's
                                    minted shape)
- PromptIdRequiredError     → 422 (kind=prompt but prompt_id missing)
- PromptNotAccessibleError  → 403 (prompt missing / not caller's / group mismatch)
- PromptNotActiveError      → 409 (prompt already responded / late / missed,
                                    or another confirm raced and won)
- PromptExpiredError        → 410 (server receipt past late_deadline; no post)
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import JSONResponse
from supabase import Client

from app.auth.jwt import get_current_user_id
from app.clients.supabase import get_supabase
from app.models.post import (
    ConfirmPostRequest,
    Post,
    PostWithMediaUrl,
    UploadUrlRequest,
    UploadUrlResponse,
)
from app.services import posts as posts_service

router = APIRouter(prefix="/posts", tags=["posts"])


def _group_locked_response() -> JSONResponse:
    """409 with the machine-readable body the client matches on.

    Deliberately {"error": "group_locked"} rather than FastAPI's default
    {"detail": ...} shape — the mobile app keys its locked-group handling off
    this exact code.
    """
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"error": "group_locked"},
    )


@router.post("/upload-url", response_model=UploadUrlResponse)
def create_upload_url(
    payload: UploadUrlRequest,
    user_id: UUID = Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
) -> UploadUrlResponse | JSONResponse:
    try:
        return posts_service.create_upload_url(sb, user_id, payload)
    except posts_service.GroupNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        ) from e
    except posts_service.GroupLockedError:
        return _group_locked_response()


@router.post("/confirm", response_model=Post)
def confirm_post(
    payload: ConfirmPostRequest,
    user_id: UUID = Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
) -> Post | JSONResponse:
    try:
        return posts_service.confirm(sb, user_id, payload)
    except posts_service.GroupNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        ) from e
    except posts_service.GroupLockedError:
        return _group_locked_response()
    except posts_service.PostNotAccessibleError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot confirm this post",
        ) from e
    except posts_service.StoragePathMismatchError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="storage_path does not match expected shape",
        ) from e
    except posts_service.MediaObjectMissingError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Media object not found at storage_path",
        ) from e
    except posts_service.PromptIdRequiredError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="prompt_id is required when kind=prompt",
        ) from e
    except posts_service.PromptNotAccessibleError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot confirm this prompt",
        ) from e
    except posts_service.PromptNotActiveError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Prompt is not active",
        ) from e
    except posts_service.PromptExpiredError as e:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Prompt window has expired",
        ) from e


@router.get("/{post_id}", response_model=PostWithMediaUrl)
def get_post(
    post_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
) -> PostWithMediaUrl:
    try:
        return posts_service.get_post(sb, user_id, post_id)
    except posts_service.PostNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Post not found",
        ) from e
    except posts_service.PostNotAccessibleError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not a member of this post's group",
        ) from e


@router.delete("/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_post(
    post_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
) -> Response:
    try:
        posts_service.delete_post(sb, user_id, post_id)
    except posts_service.PostNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Post not found",
        ) from e
    except posts_service.PostNotAccessibleError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the author can delete this post",
        ) from e
    return Response(status_code=status.HTTP_204_NO_CONTENT)
