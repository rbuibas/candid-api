"""Unit tests for /prompts/active and GET /prompts/{id}."""

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
RWS = 300  # response window seconds
LWS = 1800  # late window seconds


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


def _prompt_row(
    user_id: UUID,
    *,
    dispatched_offset_seconds: int = 0,
    status: str = "active",
    dispatched_at: datetime | None = None,
    media_type: str = "photo",
    target_video_length_seconds: int | None = None,
    rws: int = RWS,
    lws: int = LWS,
) -> dict[str, Any]:
    if dispatched_at is None and status != "scheduled":
        dispatched_at = datetime.now(UTC) + timedelta(seconds=dispatched_offset_seconds)
    return {
        "id": str(uuid4()),
        "group_id": str(uuid4()),
        "user_id": str(user_id),
        "scheduled_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        "dispatched_at": dispatched_at.isoformat() if dispatched_at else None,
        "local_date": "2026-05-30",
        "media_type": media_type,
        "target_video_length_seconds": target_video_length_seconds,
        "status": status,
        "created_at": "2026-05-30T00:00:00+00:00",
        "groups": {
            "response_window_seconds": rws,
            "late_window_seconds": lws,
        },
    }


@pytest.fixture
def fake_sb() -> MagicMock:
    return MagicMock()


@pytest.fixture
def auth_client(fake_sb: MagicMock) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(supabase_jwt_secret=TEST_SECRET)
    app.dependency_overrides[get_supabase] = lambda: fake_sb
    return TestClient(app)


def _stub_active_list(fake_sb: MagicMock, rows: list[dict[str, Any]]) -> None:
    """Configure the .table().select().eq().eq().not_.is_().execute() chain."""
    chain = fake_sb.table.return_value.select.return_value.eq.return_value.eq.return_value
    leaf = chain.not_.is_.return_value
    result = MagicMock()
    result.data = rows
    leaf.execute.return_value = result


def _stub_single_lookup(fake_sb: MagicMock, row: dict[str, Any] | None) -> None:
    """Configure the .table().select().eq().maybe_single().execute() chain."""
    chain = fake_sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value
    result = MagicMock()
    result.data = row
    chain.execute.return_value = result


# --- GET /prompts/active ---------------------------------------------


def test_active_returns_active_state_for_fresh_dispatch(
    auth_client: TestClient, fake_sb: MagicMock
) -> None:
    user_id = uuid4()
    rows = [_prompt_row(user_id, dispatched_offset_seconds=-10)]
    _stub_active_list(fake_sb, rows)

    response = auth_client.get(
        "/prompts/active",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["state"] == "active"
    assert "on_time_deadline" in body[0]
    assert "late_deadline" in body[0]


def test_active_returns_late_state_inside_late_window(
    auth_client: TestClient, fake_sb: MagicMock
) -> None:
    user_id = uuid4()
    # dispatched 10 minutes ago → past on-time (5min) but within late_window (30min)
    rows = [_prompt_row(user_id, dispatched_offset_seconds=-600)]
    _stub_active_list(fake_sb, rows)

    response = auth_client.get(
        "/prompts/active",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["state"] == "late"


def test_active_excludes_missed_state(auth_client: TestClient, fake_sb: MagicMock) -> None:
    user_id = uuid4()
    # dispatched >> rws + lws ago → expirer hasn't caught up
    rows = [_prompt_row(user_id, dispatched_offset_seconds=-(RWS + LWS + 60))]
    _stub_active_list(fake_sb, rows)

    response = auth_client.get(
        "/prompts/active",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
    )

    assert response.status_code == 200
    assert response.json() == []


def test_active_filters_by_caller_and_status_via_query(
    auth_client: TestClient, fake_sb: MagicMock
) -> None:
    user_id = uuid4()
    _stub_active_list(fake_sb, [])

    response = auth_client.get(
        "/prompts/active",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
    )

    assert response.status_code == 200
    # First eq: user_id; second eq (chained on the first eq's return): status='active'.
    fake_sb.table.assert_any_call("prompts")
    fake_sb.table.return_value.select.return_value.eq.assert_called_with("user_id", str(user_id))
    fake_sb.table.return_value.select.return_value.eq.return_value.eq.assert_called_with(
        "status", "active"
    )


def test_active_requires_auth(auth_client: TestClient) -> None:
    response = auth_client.get("/prompts/active")
    assert response.status_code == 401


# --- GET /prompts/{id} -----------------------------------------------


def test_get_single_404_when_missing(auth_client: TestClient, fake_sb: MagicMock) -> None:
    user_id = uuid4()
    _stub_single_lookup(fake_sb, None)

    response = auth_client.get(
        f"/prompts/{uuid4()}",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
    )
    assert response.status_code == 404


def test_get_single_403_for_other_users_prompt(auth_client: TestClient, fake_sb: MagicMock) -> None:
    caller = uuid4()
    other = uuid4()
    _stub_single_lookup(fake_sb, _prompt_row(other, dispatched_offset_seconds=-10))

    response = auth_client.get(
        f"/prompts/{uuid4()}",
        headers={"Authorization": f"Bearer {_mint(caller)}"},
    )
    assert response.status_code == 403


def test_get_single_409_when_not_yet_dispatched(
    auth_client: TestClient, fake_sb: MagicMock
) -> None:
    user_id = uuid4()
    row = _prompt_row(user_id, status="scheduled", dispatched_at=None)
    _stub_single_lookup(fake_sb, row)

    response = auth_client.get(
        f"/prompts/{uuid4()}",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
    )
    assert response.status_code == 409


def test_get_single_returns_view_for_responded_prompt(
    auth_client: TestClient, fake_sb: MagicMock
) -> None:
    user_id = uuid4()
    row = _prompt_row(user_id, dispatched_offset_seconds=-10, status="responded")
    _stub_single_lookup(fake_sb, row)

    response = auth_client.get(
        f"/prompts/{row['id']}",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == row["id"]
    # State reflects NOW vs dispatched_at, not the DB status.
    assert body["state"] in {"active", "late", "missed"}


def test_get_single_requires_auth(auth_client: TestClient) -> None:
    response = auth_client.get(f"/prompts/{uuid4()}")
    assert response.status_code == 401
