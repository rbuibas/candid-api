"""/posts router.

Thin handlers — the work lives in services/posts.py. The error translation
table:

- GroupNotFoundError       → 404 (caller not in group, or no such group)
- PostNotFoundError        → 404 (no such post / tombstoned)
- PostNotAccessibleError   → 403 (post exists, caller not in its group;
                                   or tampered re-confirm)
- MediaObjectMissingError  → 422 (client never PUT the bytes)
- StoragePathMismatchError → 422 (storage_path doesn't match the server's
                                   minted shape)
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
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


@router.post("/upload-url", response_model=UploadUrlResponse)
def create_upload_url(
    payload: UploadUrlRequest,
    user_id: UUID = Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
) -> UploadUrlResponse:
    try:
        return posts_service.create_upload_url(sb, user_id, payload)
    except posts_service.GroupNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        ) from e


@router.post("/confirm", response_model=Post)
def confirm_post(
    payload: ConfirmPostRequest,
    user_id: UUID = Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
) -> Post:
    try:
        return posts_service.confirm(sb, user_id, payload)
    except posts_service.GroupNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        ) from e
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
