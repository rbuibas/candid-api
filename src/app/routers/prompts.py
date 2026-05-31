"""/prompts router.

Read endpoints only — POST /dev/fire-prompt lives in routers/dev.py to keep
the gating logic obvious. Error mapping:

- PromptNotFoundError       → 404 (no such prompt)
- PromptNotAccessibleError  → 403 (caller doesn't own it)
- PromptNotDispatchedError  → 409 (still scheduled; no deadline yet)
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client

from app.auth.jwt import get_current_user_id
from app.clients.supabase import get_supabase
from app.models.prompt import PromptView
from app.services import prompts as prompts_service

router = APIRouter(prefix="/prompts", tags=["prompts"])


@router.get("/active", response_model=list[PromptView])
def list_active(
    user_id: UUID = Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
) -> list[PromptView]:
    return prompts_service.get_active_for_user(sb, user_id)


@router.get("/{prompt_id}", response_model=PromptView)
def get_prompt(
    prompt_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
) -> PromptView:
    try:
        return prompts_service.get_for_user(sb, user_id, prompt_id)
    except prompts_service.PromptNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prompt not found",
        ) from e
    except prompts_service.PromptNotAccessibleError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot read this prompt",
        ) from e
    except prompts_service.PromptNotDispatchedError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Prompt has not been dispatched yet",
        ) from e
