"""/devices router.

POST /devices/register upserts the caller's FCM token (on_conflict transfers
ownership across users for a shared device).

DELETE /devices/{fcm_token} removes a token the caller owns. Unknown or
foreign tokens come back as 404 — same anti-leak shape as the groups router.
"""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from supabase import Client

from app.auth.jwt import get_current_user_id
from app.clients.supabase import get_supabase
from app.models.device import Device, DeviceRegisterRequest
from app.services import devices as devices_service

router = APIRouter(prefix="/devices", tags=["devices"])


@router.post("/register", response_model=Device)
def register_device(
    payload: DeviceRegisterRequest,
    user_id=Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
) -> Device:
    return devices_service.register(sb, user_id, payload)


@router.delete("/{fcm_token}", status_code=status.HTTP_204_NO_CONTENT)
def delete_device(
    fcm_token: str,
    user_id=Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
) -> Response:
    try:
        devices_service.delete_for_user(sb, user_id, fcm_token)
    except devices_service.DeviceNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found",
        ) from e
    return Response(status_code=status.HTTP_204_NO_CONTENT)
