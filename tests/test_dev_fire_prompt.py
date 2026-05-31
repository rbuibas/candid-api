"""Unit tests for POST /dev/fire-prompt."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from app.clients.firebase import SendResult
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


class TableRouter:
    def __init__(self) -> None:
        self.tables: dict[str, MagicMock] = {}
        self.select_data: dict[str, Any] = {}
        self.insert_calls: dict[str, list[dict]] = {}

    def __call__(self, name: str) -> MagicMock:
        if name not in self.tables:
            self.tables[name] = self._build(name)
        return self.tables[name]

    def _build(self, name: str) -> MagicMock:
        t = MagicMock(name=f"table[{name}]")
        chain = MagicMock()

        def _passthrough(*_a: Any, **_k: Any) -> MagicMock:
            return chain

        t.select.return_value = chain
        chain.eq.side_effect = _passthrough
        chain.lte.side_effect = _passthrough
        chain.gte.side_effect = _passthrough
        chain.is_.side_effect = _passthrough
        chain.not_.is_.side_effect = _passthrough
        chain.maybe_single.return_value = chain
        result = MagicMock()
        result.data = self.select_data.get(name)
        chain.execute.return_value = result
        t._select_result = result

        self.insert_calls.setdefault(name, [])

        def _capture_insert(row: dict, _name: str = name) -> MagicMock:
            self.insert_calls[_name].append(row)
            # Real PostgREST returns the inserted row with DB-default columns
            # filled in. Synthesise created_at so Prompt.model_validate works.
            saved = {**row, "created_at": "2026-05-30T00:00:00+00:00"}
            inner = MagicMock()
            inner.execute.return_value = MagicMock(data=[saved])
            return inner

        t.insert.side_effect = _capture_insert

        def _capture_delete() -> MagicMock:
            inner = MagicMock()
            inner.in_.return_value.execute.return_value = MagicMock(data=[])
            return inner

        t.delete.side_effect = _capture_delete
        return t

    def stub_select(self, name: str, data: Any) -> None:
        self.select_data[name] = data
        t = self.__call__(name)
        t._select_result.data = data


@pytest.fixture
def router() -> TableRouter:
    return TableRouter()


@pytest.fixture
def fake_sb(router: TableRouter) -> MagicMock:
    sb = MagicMock()
    sb.table.side_effect = router
    return sb


@pytest.fixture
def stub_send_push(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    m = MagicMock(return_value=SendResult(1, 0, []))
    monkeypatch.setattr("app.services.prompts.firebase.send_push", m)
    return m


def _build_client(fake_sb: MagicMock, *, dev_enabled: bool) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        supabase_jwt_secret=TEST_SECRET, dev_endpoints_enabled=dev_enabled
    )
    app.dependency_overrides[get_supabase] = lambda: fake_sb
    return TestClient(app)


# --- Gating -----------------------------------------------------


def test_fire_prompt_disabled_returns_404(fake_sb: MagicMock) -> None:
    client = _build_client(fake_sb, dev_enabled=False)
    response = client.post(
        "/dev/fire-prompt",
        headers={"Authorization": f"Bearer {_mint(uuid4())}"},
        json={"group_id": str(uuid4())},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Not Found"


def test_fire_prompt_requires_auth(fake_sb: MagicMock) -> None:
    client = _build_client(fake_sb, dev_enabled=True)
    response = client.post("/dev/fire-prompt", json={"group_id": str(uuid4())})
    assert response.status_code == 401


# --- Happy paths ----------------------------------------------


def test_fire_prompt_inserts_active_prompt(
    router: TableRouter, fake_sb: MagicMock, stub_send_push: MagicMock
) -> None:
    user_id = uuid4()
    group_id = uuid4()
    router.stub_select("group_members", {"id": "membership-row-id"})
    router.stub_select(
        "groups",
        {
            "max_video_length_seconds": 10,
            "response_window_seconds": 300,
            "late_window_seconds": 1800,
        },
    )
    router.stub_select("profiles", {"timezone": "Europe/Bucharest"})
    router.stub_select("devices", [{"fcm_token": "tok-1"}])
    client = _build_client(fake_sb, dev_enabled=True)

    response = client.post(
        "/dev/fire-prompt",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={"group_id": str(group_id), "media_type": "photo"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "active"
    assert body["dispatched_at"] is not None
    assert body["scheduled_at"] is not None
    assert body["media_type"] == "photo"
    assert body["user_id"] == str(user_id)
    assert body["group_id"] == str(group_id)
    assert len(router.insert_calls["prompts"]) == 1


def test_fire_prompt_sends_push_to_caller_devices(
    router: TableRouter, fake_sb: MagicMock, stub_send_push: MagicMock
) -> None:
    user_id = uuid4()
    group_id = uuid4()
    router.stub_select("group_members", {"id": "membership-row-id"})
    router.stub_select(
        "groups",
        {
            "max_video_length_seconds": 10,
            "response_window_seconds": 300,
            "late_window_seconds": 1800,
        },
    )
    router.stub_select("profiles", {"timezone": "UTC"})
    router.stub_select("devices", [{"fcm_token": "tok-a"}, {"fcm_token": "tok-b"}])
    client = _build_client(fake_sb, dev_enabled=True)

    client.post(
        "/dev/fire-prompt",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={"group_id": str(group_id), "media_type": "video"},
    )

    stub_send_push.assert_called_once()
    tokens, data = stub_send_push.call_args.args[0], stub_send_push.call_args.args[1]
    assert tokens == ["tok-a", "tok-b"]
    assert data["group_id"] == str(group_id)
    assert data["media_type"] == "video"
    assert "target_video_length_seconds" in data
    assert stub_send_push.call_args.kwargs["title"] == "Time to capture"


def test_fire_prompt_video_target_length_in_range(
    router: TableRouter, fake_sb: MagicMock, stub_send_push: MagicMock
) -> None:
    user_id = uuid4()
    group_id = uuid4()
    router.stub_select("group_members", {"id": "membership-row-id"})
    router.stub_select(
        "groups",
        {
            "max_video_length_seconds": 10,
            "response_window_seconds": 300,
            "late_window_seconds": 1800,
        },
    )
    router.stub_select("profiles", {"timezone": "UTC"})
    router.stub_select("devices", [])
    client = _build_client(fake_sb, dev_enabled=True)

    response = client.post(
        "/dev/fire-prompt",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={"group_id": str(group_id), "media_type": "video"},
    )
    assert response.status_code == 200
    target = response.json()["target_video_length_seconds"]
    assert target is not None
    assert 3 <= target <= 10


def test_fire_prompt_non_member_returns_404(
    router: TableRouter, fake_sb: MagicMock, stub_send_push: MagicMock
) -> None:
    user_id = uuid4()
    router.stub_select("group_members", None)
    client = _build_client(fake_sb, dev_enabled=True)

    response = client.post(
        "/dev/fire-prompt",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={"group_id": str(uuid4())},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Group not found"
