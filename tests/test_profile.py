"""Integration tests for /profile.

Uses TestClient with `app.dependency_overrides` to swap in a Settings
with a known JWT secret and a stub Supabase client. No live DB.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import get_current_user_id  # noqa: F401  (registered as a dep)
from app.clients.supabase import get_supabase
from app.config import Settings, get_settings
from app.main import create_app

TEST_SECRET = "test-jwt-secret-32-bytes-or-more-aaaaaaaa"


def _mint(user_id: UUID) -> str:
    now = datetime.now(tz=UTC)
    return jwt.encode(
        {
            "sub": str(user_id),
            "aud": "authenticated",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        TEST_SECRET,
        algorithm="HS256",
    )


def _profile_row(user_id: UUID, **overrides: Any) -> dict[str, Any]:
    base = {
        "id": str(user_id),
        "display_name": None,
        "avatar_url": None,
        "timezone": "UTC",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    base.update(overrides)
    return base


@pytest.fixture
def fake_sb() -> MagicMock:
    return MagicMock()


@pytest.fixture
def auth_client(fake_sb: MagicMock) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(supabase_jwt_secret=TEST_SECRET)
    app.dependency_overrides[get_supabase] = lambda: fake_sb
    return TestClient(app)


# --- GET /profile ----------------------------------------------------


def test_get_profile_returns_row(auth_client: TestClient, fake_sb: MagicMock) -> None:
    user_id = uuid4()
    row = _profile_row(user_id, timezone="America/Los_Angeles")
    fake_sb.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value.data = row  # noqa: E501

    response = auth_client.get(
        "/profile",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
    )

    assert response.status_code == 200
    assert response.json()["id"] == str(user_id)
    assert response.json()["timezone"] == "America/Los_Angeles"
    fake_sb.table.assert_called_with("profiles")
    fake_sb.table.return_value.select.return_value.eq.assert_called_with("id", str(user_id))


def test_get_profile_without_auth_returns_401(auth_client: TestClient) -> None:
    response = auth_client.get("/profile")
    assert response.status_code == 401


def test_get_profile_with_bad_jwt_returns_401(auth_client: TestClient) -> None:
    response = auth_client.get("/profile", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


def test_get_profile_missing_row_returns_404(auth_client: TestClient, fake_sb: MagicMock) -> None:
    user_id = uuid4()
    fake_sb.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value.data = None  # noqa: E501

    response = auth_client.get(
        "/profile",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
    )

    assert response.status_code == 404


# --- PATCH /profile --------------------------------------------------


def test_patch_profile_updates_timezone(auth_client: TestClient, fake_sb: MagicMock) -> None:
    user_id = uuid4()
    updated = _profile_row(user_id, timezone="America/Los_Angeles")
    fake_sb.table.return_value.update.return_value.eq.return_value.execute.return_value.data = [
        updated
    ]

    response = auth_client.patch(
        "/profile",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={"timezone": "America/Los_Angeles"},
    )

    assert response.status_code == 200
    assert response.json()["timezone"] == "America/Los_Angeles"
    fake_sb.table.return_value.update.assert_called_with({"timezone": "America/Los_Angeles"})
    fake_sb.table.return_value.update.return_value.eq.assert_called_with("id", str(user_id))


def test_patch_profile_only_sends_set_fields(auth_client: TestClient, fake_sb: MagicMock) -> None:
    """exclude_unset means a missing field is NOT passed to the DB (so the
    DB-level default / existing value is preserved). Sending null IS passed."""
    user_id = uuid4()
    fake_sb.table.return_value.update.return_value.eq.return_value.execute.return_value.data = [
        _profile_row(user_id, display_name="Raul")
    ]

    response = auth_client.patch(
        "/profile",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={"display_name": "Raul"},
    )

    assert response.status_code == 200
    fake_sb.table.return_value.update.assert_called_with({"display_name": "Raul"})


def test_patch_profile_empty_body_returns_400(auth_client: TestClient, fake_sb: MagicMock) -> None:
    user_id = uuid4()
    response = auth_client.patch(
        "/profile",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={},
    )
    assert response.status_code == 400
    fake_sb.table.return_value.update.assert_not_called()


def test_patch_profile_without_auth_returns_401(auth_client: TestClient) -> None:
    response = auth_client.patch("/profile", json={"timezone": "UTC"})
    assert response.status_code == 401
