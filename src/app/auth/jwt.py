"""Supabase JWT verification.

Two dependencies:

- `get_current_user_id` — fast, no DB. Verifies the HS256 signature against
  `SUPABASE_JWT_SECRET`, returns the `sub` claim as a UUID. Use this on routes
  that only need the user's id (e.g. PATCH /profile/me, where the existing row
  doesn't need to be loaded before overwriting).

- `get_current_user` — depends on the above + does a single Supabase lookup,
  returns the full `Profile`. Use this on routes that already need the row.

`_verify_jwt` is the pure function the two share. It takes no Depends-time
side effects, so unit tests call it directly with hand-built args.

RLS in Postgres is the second line of defense; the service-role client the API
uses bypasses it, so every query must still scope by the returned user id.
"""

from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from supabase import Client

from app.clients.supabase import get_supabase
from app.config import Settings, get_settings
from app.models.profile import Profile
from app.services import profile as profile_service

bearer_scheme = HTTPBearer(auto_error=False)


def _verify_jwt(
    credentials: HTTPAuthorizationCredentials | None,
    settings: Settings,
) -> UUID:
    """Verify the bearer token and return its `sub` claim as a UUID.

    Pure function — no I/O, no Depends-time effects. Raises HTTPException
    on every failure mode; callers re-raise as-is.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not settings.supabase_jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server JWT secret not configured",
        )

    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            audience="authenticated",
        )
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from e
    except jwt.InvalidTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from e

    sub = payload.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing sub claim",
        )

    try:
        return UUID(sub)
    except (ValueError, TypeError) as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token sub is not a valid UUID",
        ) from e


def get_current_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> UUID:
    return _verify_jwt(credentials, settings)


def get_current_user(
    user_id: UUID = Depends(get_current_user_id),
    sb: Client = Depends(get_supabase),
) -> Profile:
    try:
        return profile_service.get_for_user(sb, user_id)
    except profile_service.ProfileNotFoundError as e:
        # Token is valid but the user has no profile row. Should be unreachable
        # because of the handle_new_user trigger; if it fires, treat the token
        # as effectively invalid (the user no longer exists in our world).
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User has no profile",
        ) from e
