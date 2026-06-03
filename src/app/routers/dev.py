"""/dev router — disabled in prod.

Every endpoint here returns 404 unless `DEV_ENDPOINTS_ENABLED=true`. The
gating returns 404 rather than 403 so the endpoint surface is not
discoverable from outside.

Endpoints here:
- `/dev/fire-prompt`     — create+dispatch a prompt AND send the FCM push, for
  hand-testing the push pipeline without the generator+dispatcher cron cycle.
- `/dev/prompts/trigger` — create+dispatch a prompt with NO push, returning the
  PromptView read shape. The app calls this then navigates straight to capture;
  it's a direct device-testing trigger, not a real dispatch.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client

from app.auth.jwt import get_current_user_id
from app.clients.supabase import get_supabase
from app.config import Settings, get_settings
from app.models.prompt import FirePromptRequest, Prompt, PromptView, TriggerPromptRequest
from app.services import prompts as prompts_service

router = APIRouter(prefix="/dev", tags=["dev"])


@router.post("/fire-prompt", response_model=Prompt)
def fire_prompt(
    payload: FirePromptRequest,
    user_id: UUID = Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
    settings: Settings = Depends(get_settings),
) -> Prompt:
    if not settings.dev_endpoints_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    try:
        return prompts_service.fire_prompt_now(sb, user_id, payload)
    except prompts_service.GroupNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found") from e


@router.post("/prompts/trigger", response_model=PromptView)
def trigger_prompt(
    payload: TriggerPromptRequest,
    user_id: UUID = Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
    settings: Settings = Depends(get_settings),
) -> PromptView:
    if not settings.dev_endpoints_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    try:
        return prompts_service.trigger_prompt_for_user(sb, user_id, payload)
    except prompts_service.GroupNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found") from e
