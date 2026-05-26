"""Unit tests for the JWT verification dependency.

Calls `get_current_user_id` directly with hand-built credentials and
settings so the security-critical path is exercised without TestClient
overhead. HTTP-level coverage lives in test_profile.py.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.auth.jwt import get_current_user_id
from app.config import Settings

TEST_SECRET = "test-jwt-secret-32-bytes-or-more-aaaaaaaa"


def _settings(secret: str | None = TEST_SECRET) -> Settings:
    return Settings(supabase_jwt_secret=secret)


def _mint(
    *,
    sub: str | None = None,
    secret: str = TEST_SECRET,
    audience: str = "authenticated",
    expires_in_seconds: int = 3600,
) -> str:
    now = datetime.now(tz=UTC)
    payload: dict[str, object] = {
        "aud": audience,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in_seconds)).timestamp()),
    }
    if sub is not None:
        payload["sub"] = sub
    return jwt.encode(payload, secret, algorithm="HS256")


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_valid_token_returns_uuid() -> None:
    user_id = uuid4()
    token = _mint(sub=str(user_id))
    result = get_current_user_id(credentials=_creds(token), settings=_settings())
    assert result == user_id
    assert isinstance(result, UUID)


def test_missing_credentials_returns_401() -> None:
    with pytest.raises(HTTPException) as exc:
        get_current_user_id(credentials=None, settings=_settings())
    assert exc.value.status_code == 401
    assert "Missing" in exc.value.detail


def test_server_secret_not_configured_returns_500() -> None:
    token = _mint(sub=str(uuid4()))
    with pytest.raises(HTTPException) as exc:
        get_current_user_id(credentials=_creds(token), settings=_settings(secret=None))
    assert exc.value.status_code == 500


def test_expired_token_returns_401() -> None:
    token = _mint(sub=str(uuid4()), expires_in_seconds=-60)
    with pytest.raises(HTTPException) as exc:
        get_current_user_id(credentials=_creds(token), settings=_settings())
    assert exc.value.status_code == 401
    assert "expired" in exc.value.detail.lower()


def test_wrong_signature_returns_401() -> None:
    token = _mint(sub=str(uuid4()), secret="someone-else-signed-this-aaaaaaaaaaaaaa")
    with pytest.raises(HTTPException) as exc:
        get_current_user_id(credentials=_creds(token), settings=_settings())
    assert exc.value.status_code == 401


def test_wrong_audience_returns_401() -> None:
    token = _mint(sub=str(uuid4()), audience="anon")
    with pytest.raises(HTTPException) as exc:
        get_current_user_id(credentials=_creds(token), settings=_settings())
    assert exc.value.status_code == 401


def test_missing_sub_returns_401() -> None:
    token = _mint(sub=None)
    with pytest.raises(HTTPException) as exc:
        get_current_user_id(credentials=_creds(token), settings=_settings())
    assert exc.value.status_code == 401


def test_non_uuid_sub_returns_401() -> None:
    token = _mint(sub="not-a-uuid")
    with pytest.raises(HTTPException) as exc:
        get_current_user_id(credentials=_creds(token), settings=_settings())
    assert exc.value.status_code == 401


def test_malformed_token_returns_401() -> None:
    with pytest.raises(HTTPException) as exc:
        get_current_user_id(credentials=_creds("not.a.jwt"), settings=_settings())
    assert exc.value.status_code == 401
