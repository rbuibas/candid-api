"""Integration tests for /profile/me.

Uses TestClient with `app.dependency_overrides` to swap in a Settings
with a known JWT secret and a stub Supabase client. No live DB.

The fake supabase needs to satisfy two distinct chains:
- `.table().select().eq().single().execute()` — for `get_current_user`
  on every request (this is the profile load that the auth dep does).
- `.table().update().eq().execute()` — for PATCH /me's service call.

MagicMock's auto-chaining handles both: each `return_value` is a fresh
MagicMock with its own `.data`. Configure the chain you care about per test.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

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


def _stub_select(fake_sb: MagicMock, row: dict[str, Any] | None) -> None:
    """Configure the .table().select().eq().maybe_single().execute() chain."""
    fake_sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = row  # noqa: E501


def _stub_update(fake_sb: MagicMock, rows: list[dict[str, Any]]) -> None:
    """Configure the .table().update().eq().execute() chain."""
    fake_sb.table.return_value.update.return_value.eq.return_value.execute.return_value.data = rows


@pytest.fixture
def fake_sb() -> MagicMock:
    return MagicMock()


@pytest.fixture
def auth_client(fake_sb: MagicMock) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(supabase_jwt_secret=TEST_SECRET)
    app.dependency_overrides[get_supabase] = lambda: fake_sb
    return TestClient(app)


# --- GET /profile/me -------------------------------------------------


def test_get_me_returns_row(auth_client: TestClient, fake_sb: MagicMock) -> None:
    user_id = uuid4()
    _stub_select(fake_sb, _profile_row(user_id, timezone="America/Los_Angeles"))

    response = auth_client.get(
        "/profile/me",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
    )

    assert response.status_code == 200
    assert response.json()["id"] == str(user_id)
    assert response.json()["timezone"] == "America/Los_Angeles"
    fake_sb.table.assert_any_call("profiles")
    fake_sb.table.return_value.select.return_value.eq.assert_called_with("id", str(user_id))


def test_get_me_without_auth_returns_401(auth_client: TestClient) -> None:
    response = auth_client.get("/profile/me")
    assert response.status_code == 401


def test_get_me_with_bad_jwt_returns_401(auth_client: TestClient) -> None:
    response = auth_client.get("/profile/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


def test_get_me_missing_profile_returns_401(auth_client: TestClient, fake_sb: MagicMock) -> None:
    """Token valid but no profiles row — should be unreachable in normal flow
    because of the handle_new_user trigger, but if it fires the route surfaces
    it as 401 (not 404) so the user logs out and back in cleanly."""
    user_id = uuid4()
    _stub_select(fake_sb, None)

    response = auth_client.get(
        "/profile/me",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
    )
    assert response.status_code == 401


# --- PATCH /profile/me -----------------------------------------------


def test_patch_me_updates_timezone(auth_client: TestClient, fake_sb: MagicMock) -> None:
    user_id = uuid4()
    _stub_update(fake_sb, [_profile_row(user_id, timezone="America/Los_Angeles")])

    response = auth_client.patch(
        "/profile/me",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={"timezone": "America/Los_Angeles"},
    )

    assert response.status_code == 200
    assert response.json()["timezone"] == "America/Los_Angeles"
    fake_sb.table.return_value.update.assert_called_with({"timezone": "America/Los_Angeles"})
    fake_sb.table.return_value.update.return_value.eq.assert_called_with("id", str(user_id))


def test_patch_me_only_sends_set_fields(auth_client: TestClient, fake_sb: MagicMock) -> None:
    """exclude_unset means a missing field is NOT passed to the DB (so the
    DB-level default / existing value is preserved). Sending null IS passed."""
    user_id = uuid4()
    _stub_update(fake_sb, [_profile_row(user_id, display_name="Raul")])

    response = auth_client.patch(
        "/profile/me",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={"display_name": "Raul"},
    )

    assert response.status_code == 200
    fake_sb.table.return_value.update.assert_called_with({"display_name": "Raul"})


def test_patch_me_empty_body_returns_400(auth_client: TestClient, fake_sb: MagicMock) -> None:
    user_id = uuid4()
    response = auth_client.patch(
        "/profile/me",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={},
    )
    assert response.status_code == 400
    fake_sb.table.return_value.update.assert_not_called()


def test_patch_me_without_auth_returns_401(auth_client: TestClient) -> None:
    response = auth_client.patch("/profile/me", json={"timezone": "UTC"})
    assert response.status_code == 401


def test_patch_me_missing_profile_returns_404(auth_client: TestClient, fake_sb: MagicMock) -> None:
    user_id = uuid4()
    _stub_update(fake_sb, [])  # update affected 0 rows

    response = auth_client.patch(
        "/profile/me",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={"timezone": "UTC"},
    )
    assert response.status_code == 404
