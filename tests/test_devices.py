"""Unit tests for /devices/register and DELETE /devices/{fcm_token}.

Auth uses get_current_user_id which only verifies the JWT — no DB lookup —
so the fake Supabase only needs to satisfy the devices chains.
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


def _device_row(user_id: UUID, **overrides: Any) -> dict[str, Any]:
    base = {
        "id": str(uuid4()),
        "user_id": str(user_id),
        "fcm_token": "tok-abc",
        "platform": "android",
        "last_seen_at": "2026-05-30T12:00:00+00:00",
        "created_at": "2026-05-30T12:00:00+00:00",
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


# --- POST /devices/register -----------------------------------------


def test_register_upserts_and_returns_row(auth_client: TestClient, fake_sb: MagicMock) -> None:
    user_id = uuid4()
    upsert_result = MagicMock()
    upsert_result.data = [_device_row(user_id, fcm_token="tok-xyz", platform="ios")]
    fake_sb.table.return_value.upsert.return_value.execute.return_value = upsert_result

    response = auth_client.post(
        "/devices/register",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={"fcm_token": "tok-xyz", "platform": "ios"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["fcm_token"] == "tok-xyz"
    assert body["platform"] == "ios"
    assert body["user_id"] == str(user_id)

    upsert_call = fake_sb.table.return_value.upsert.call_args
    assert upsert_call.kwargs["on_conflict"] == "fcm_token"
    row = upsert_call.args[0]
    assert row["fcm_token"] == "tok-xyz"
    assert row["platform"] == "ios"
    assert row["user_id"] == str(user_id)
    assert "last_seen_at" in row


def test_register_overwrites_user_id_on_shared_device(
    auth_client: TestClient, fake_sb: MagicMock
) -> None:
    """The upsert on_conflict=fcm_token transfers ownership to the new caller."""
    new_user = uuid4()
    upsert_result = MagicMock()
    upsert_result.data = [_device_row(new_user, fcm_token="shared-tok", platform="android")]
    fake_sb.table.return_value.upsert.return_value.execute.return_value = upsert_result

    response = auth_client.post(
        "/devices/register",
        headers={"Authorization": f"Bearer {_mint(new_user)}"},
        json={"fcm_token": "shared-tok", "platform": "android"},
    )

    assert response.status_code == 200
    assert response.json()["user_id"] == str(new_user)


def test_register_requires_auth(auth_client: TestClient) -> None:
    response = auth_client.post(
        "/devices/register",
        json={"fcm_token": "tok", "platform": "android"},
    )
    assert response.status_code == 401


def test_register_rejects_invalid_platform(auth_client: TestClient) -> None:
    user_id = uuid4()
    response = auth_client.post(
        "/devices/register",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={"fcm_token": "tok", "platform": "windows"},
    )
    assert response.status_code == 422


# --- DELETE /devices/{fcm_token} ------------------------------------


def _stub_select_owner(fake_sb: MagicMock, owner_user_id: UUID | None) -> None:
    """Configure the .table().select().eq().maybe_single().execute() chain."""
    chain = fake_sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value
    result = MagicMock()
    result.data = {"user_id": str(owner_user_id)} if owner_user_id else None
    chain.execute.return_value = result


def test_delete_204_on_own_token(auth_client: TestClient, fake_sb: MagicMock) -> None:
    user_id = uuid4()
    _stub_select_owner(fake_sb, user_id)

    response = auth_client.delete(
        "/devices/tok-mine",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
    )

    assert response.status_code == 204
    fake_sb.table.return_value.delete.return_value.eq.assert_called_with("fcm_token", "tok-mine")


def test_delete_404_on_other_users_token(auth_client: TestClient, fake_sb: MagicMock) -> None:
    caller = uuid4()
    other = uuid4()
    _stub_select_owner(fake_sb, other)

    response = auth_client.delete(
        "/devices/tok-other",
        headers={"Authorization": f"Bearer {_mint(caller)}"},
    )

    assert response.status_code == 404
    fake_sb.table.return_value.delete.assert_not_called()


def test_delete_404_on_unknown_token(auth_client: TestClient, fake_sb: MagicMock) -> None:
    user_id = uuid4()
    _stub_select_owner(fake_sb, None)

    response = auth_client.delete(
        "/devices/tok-nope",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
    )

    assert response.status_code == 404


def test_delete_requires_auth(auth_client: TestClient) -> None:
    response = auth_client.delete("/devices/tok")
    assert response.status_code == 401
