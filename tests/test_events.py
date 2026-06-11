"""Unit tests for POST /events.

Auth uses get_current_user_id (JWT only, no DB lookup), so the fake Supabase
just needs to satisfy two chains: the membership probe
(.table().select().eq().eq().maybe_single().execute()) and the insert
(.table().insert().execute()). Mirrors tests/test_devices.py.
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


@pytest.fixture
def fake_sb() -> MagicMock:
    return MagicMock()


@pytest.fixture
def auth_client(fake_sb: MagicMock) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(supabase_jwt_secret=TEST_SECRET)
    app.dependency_overrides[get_supabase] = lambda: fake_sb
    return TestClient(app)


def _stub_member(fake_sb: MagicMock, *, is_member: bool) -> None:
    """Configure the membership probe chain to report (non-)membership."""
    # .table().select().eq().eq().maybe_single().execute()
    eq_chain = fake_sb.table.return_value.select.return_value.eq.return_value.eq.return_value
    chain = eq_chain.maybe_single.return_value
    result = MagicMock()
    result.data = {"id": str(uuid4())} if is_member else None
    chain.execute.return_value = result


def _stub_insert(fake_sb: MagicMock, row: dict[str, Any]) -> None:
    insert_result = MagicMock()
    insert_result.data = [row]
    fake_sb.table.return_value.insert.return_value.execute.return_value = insert_result


def _event_row(group_id: UUID, user_id: UUID, **overrides: Any) -> dict[str, Any]:
    base = {
        "id": str(uuid4()),
        "group_id": str(group_id),
        "user_id": str(user_id),
        "name": "feed_opened",
        "payload": {"source": "standalone"},
        "created_at": "2026-06-10T12:00:00+00:00",
    }
    base.update(overrides)
    return base


def test_record_event_201_for_member(auth_client: TestClient, fake_sb: MagicMock) -> None:
    user_id = uuid4()
    group_id = uuid4()
    _stub_member(fake_sb, is_member=True)
    _stub_insert(fake_sb, _event_row(group_id, user_id))

    response = auth_client.post(
        "/events",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={
            "group_id": str(group_id),
            "name": "feed_opened",
            "payload": {"source": "standalone"},
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "feed_opened"
    assert body["payload"] == {"source": "standalone"}
    assert body["user_id"] == str(user_id)
    assert body["group_id"] == str(group_id)

    # The inserted row is owned by the caller, never the client-supplied user.
    inserted = fake_sb.table.return_value.insert.call_args.args[0]
    assert inserted["user_id"] == str(user_id)
    assert inserted["group_id"] == str(group_id)
    assert inserted["name"] == "feed_opened"
    assert inserted["payload"] == {"source": "standalone"}


def test_record_event_defaults_empty_payload(auth_client: TestClient, fake_sb: MagicMock) -> None:
    user_id = uuid4()
    group_id = uuid4()
    _stub_member(fake_sb, is_member=True)
    _stub_insert(fake_sb, _event_row(group_id, user_id, name="some_event", payload={}))

    response = auth_client.post(
        "/events",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={"group_id": str(group_id), "name": "some_event"},
    )

    assert response.status_code == 201
    assert fake_sb.table.return_value.insert.call_args.args[0]["payload"] == {}


def test_record_event_404_for_non_member(auth_client: TestClient, fake_sb: MagicMock) -> None:
    user_id = uuid4()
    group_id = uuid4()
    _stub_member(fake_sb, is_member=False)

    response = auth_client.post(
        "/events",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={"group_id": str(group_id), "name": "feed_opened"},
    )

    assert response.status_code == 404
    fake_sb.table.return_value.insert.assert_not_called()


def test_record_event_requires_auth(auth_client: TestClient) -> None:
    response = auth_client.post(
        "/events",
        json={"group_id": str(uuid4()), "name": "feed_opened"},
    )
    assert response.status_code == 401


def test_record_event_rejects_blank_name(auth_client: TestClient, fake_sb: MagicMock) -> None:
    user_id = uuid4()
    response = auth_client.post(
        "/events",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={"group_id": str(uuid4()), "name": ""},
    )
    assert response.status_code == 422


def test_record_event_rejects_overlong_name(auth_client: TestClient, fake_sb: MagicMock) -> None:
    user_id = uuid4()
    response = auth_client.post(
        "/events",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={"group_id": str(uuid4()), "name": "x" * 65},  # max_length is 64
    )
    assert response.status_code == 422
    fake_sb.table.return_value.insert.assert_not_called()


def test_record_event_accepts_max_length_name(auth_client: TestClient, fake_sb: MagicMock) -> None:
    user_id = uuid4()
    group_id = uuid4()
    name = "x" * 64  # the boundary — still valid
    _stub_member(fake_sb, is_member=True)
    _stub_insert(fake_sb, _event_row(group_id, user_id, name=name))

    response = auth_client.post(
        "/events",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={"group_id": str(group_id), "name": name},
    )
    assert response.status_code == 201
    assert fake_sb.table.return_value.insert.call_args.args[0]["name"] == name


def test_record_event_rejects_invalid_group_uuid(
    auth_client: TestClient, fake_sb: MagicMock
) -> None:
    user_id = uuid4()
    response = auth_client.post(
        "/events",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={"group_id": "not-a-uuid", "name": "feed_opened"},
    )
    assert response.status_code == 422
    fake_sb.table.return_value.insert.assert_not_called()


def test_record_event_requires_group_id(auth_client: TestClient, fake_sb: MagicMock) -> None:
    user_id = uuid4()
    response = auth_client.post(
        "/events",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={"name": "feed_opened"},
    )
    assert response.status_code == 422


def test_record_event_preserves_nested_payload(auth_client: TestClient, fake_sb: MagicMock) -> None:
    """The server stores the payload verbatim — arbitrary nested JSON survives."""
    user_id = uuid4()
    group_id = uuid4()
    payload = {"source": "standalone", "meta": {"n": 3, "tags": ["a", "b"]}}
    _stub_member(fake_sb, is_member=True)
    _stub_insert(fake_sb, _event_row(group_id, user_id, payload=payload))

    response = auth_client.post(
        "/events",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={"group_id": str(group_id), "name": "feed_opened", "payload": payload},
    )
    assert response.status_code == 201
    assert response.json()["payload"] == payload
    assert fake_sb.table.return_value.insert.call_args.args[0]["payload"] == payload
