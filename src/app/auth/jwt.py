"""Supabase JWT verification.

Supports both signing schemes Supabase uses in the wild:

- **HS256** (legacy): symmetric, shared secret in `SUPABASE_JWT_SECRET`. Used
  by older projects and configurable on Settings → API → JWT Settings.
- **ES256 / RS256** (current default for new projects): asymmetric, signed by
  Supabase with a private key whose public counterpart is published at the
  project's JWKS endpoint `<SUPABASE_URL>/auth/v1/.well-known/jwks.json`.

We peek at the JWT header's `alg` and route accordingly. PyJWKClient caches the
JWKS response (default lifespan ~24h) so we don't hit Supabase on every request.

Two dependencies expose the result:

- `get_current_user_id` — fast, no DB. Returns the `sub` claim as a UUID.
- `get_current_user` — does the DB lookup and returns the full `Profile`.

RLS in Postgres is the second line of defense; every query still scopes by user id.
"""

from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from supabase import Client

from app.clients.supabase import get_supabase
from app.config import Settings, get_settings
from app.models.profile import Profile
from app.services import profile as profile_service

bearer_scheme = HTTPBearer(auto_error=False)

_SUPPORTED_ALGS = frozenset({"HS256", "ES256", "RS256"})
_ASYMMETRIC_ALGS = frozenset({"ES256", "RS256"})

# JWKS client cache keyed by Supabase URL. Module-scoped so multiple
# requests share one cached fetch. Test code that wants isolation can
# clear it via `_jwk_clients.clear()`.
_jwk_clients: dict[str, PyJWKClient] = {}


def _jwk_client_for(supabase_url: str) -> PyJWKClient:
    cached = _jwk_clients.get(supabase_url)
    if cached is not None:
        return cached
    jwks_url = f"{supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
    client = PyJWKClient(jwks_url, cache_keys=True)
    _jwk_clients[supabase_url] = client
    return client


def _verify_jwt(
    credentials: HTTPAuthorizationCredentials | None,
    settings: Settings,
) -> UUID:
    """Verify the bearer token and return its `sub` claim as a UUID.

    Pure function — no Depends, no app state besides the JWKS cache. Callers
    re-raise the HTTPExceptions this throws.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    try:
        unverified_header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed token header",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from e

    alg = unverified_header.get("alg")
    if alg not in _SUPPORTED_ALGS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Unsupported token algorithm: {alg!r}",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )

    if alg in _ASYMMETRIC_ALGS:
        if not settings.supabase_url:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="SUPABASE_URL not configured (required for JWKS verification)",
            )
        try:
            signing_key = _jwk_client_for(settings.supabase_url).get_signing_key_from_jwt(token).key
        except jwt.PyJWKClientError as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Failed to resolve JWT signing key: {e}",
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            ) from e
    else:
        # HS256
        if not settings.supabase_jwt_secret:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="SUPABASE_JWT_SECRET not configured",
            )
        signing_key = settings.supabase_jwt_secret

    try:
        payload = jwt.decode(
            token,
            signing_key,
            algorithms=[alg],
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
        # Token verified but no profiles row. Trigger should make this
        # unreachable; surface as 401 so the client logs out cleanly.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User has no profile",
        ) from e
