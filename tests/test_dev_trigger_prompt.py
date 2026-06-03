"""Unit tests for POST /dev/prompts/trigger.

Reuses the TableRouter / fake-supabase harness shape from
test_dev_fire_prompt.py. The trigger endpoint differs from /dev/fire-prompt in
two ways under test here: it returns a PromptView (computed deadlines + UI
state) and it sends NO push.
"""

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
        chain.maybe_single.return_value = chain
        result = MagicMock()
        result.data = self.select_data.get(name)
        chain.execute.return_value = result
        t._select_result = result

        self.insert_calls.setdefault(name, [])

        def _capture_insert(row: dict, _name: str = name) -> MagicMock:
            self.insert_calls[_name].append(row)
            saved = {**row, "created_at": "2026-05-30T00:00:00+00:00"}
            inner = MagicMock()
            inner.execute.return_value = MagicMock(data=[saved])
            return inner

        t.insert.side_effect = _capture_insert
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


def _stub_group(router: TableRouter, *, tz: str = "UTC") -> None:
    router.stub_select("group_members", {"id": "membership-row-id"})
    router.stub_select(
        "groups",
        {
            "max_video_length_seconds": 10,
            "response_window_seconds": 300,
            "late_window_seconds": 1800,
        },
    )
    router.stub_select("profiles", {"timezone": tz})


# --- Gating -----------------------------------------------------


def test_trigger_disabled_returns_404(fake_sb: MagicMock) -> None:
    client = _build_client(fake_sb, dev_enabled=False)
    response = client.post(
        "/dev/prompts/trigger",
        headers={"Authorization": f"Bearer {_mint(uuid4())}"},
        json={"group_id": str(uuid4()), "media_type": "photo"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Not Found"


def test_trigger_requires_auth(fake_sb: MagicMock) -> None:
    client = _build_client(fake_sb, dev_enabled=True)
    response = client.post(
        "/dev/prompts/trigger", json={"group_id": str(uuid4()), "media_type": "photo"}
    )
    assert response.status_code == 401


def test_trigger_media_type_required(fake_sb: MagicMock) -> None:
    client = _build_client(fake_sb, dev_enabled=True)
    response = client.post(
        "/dev/prompts/trigger",
        headers={"Authorization": f"Bearer {_mint(uuid4())}"},
        json={"group_id": str(uuid4())},
    )
    assert response.status_code == 422


# --- Happy paths ----------------------------------------------


def test_trigger_returns_promptview_with_deadlines(
    router: TableRouter, fake_sb: MagicMock, stub_send_push: MagicMock
) -> None:
    user_id = uuid4()
    group_id = uuid4()
    _stub_group(router)
    client = _build_client(fake_sb, dev_enabled=True)

    response = client.post(
        "/dev/prompts/trigger",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={"group_id": str(group_id), "media_type": "photo"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # PromptView shape — no user_id/status/scheduled_at fields.
    assert set(body) == {
        "id",
        "group_id",
        "media_type",
        "target_video_length_seconds",
        "dispatched_at",
        "on_time_deadline",
        "late_deadline",
        "state",
    }
    assert body["group_id"] == str(group_id)
    assert body["media_type"] == "photo"
    assert body["state"] == "active"

    dispatched = datetime.fromisoformat(body["dispatched_at"])
    on_time = datetime.fromisoformat(body["on_time_deadline"])
    late = datetime.fromisoformat(body["late_deadline"])
    assert on_time - dispatched == timedelta(seconds=300)
    assert late - dispatched == timedelta(seconds=300 + 1800)

    # Row was inserted with the dispatched-and-actionable status, not 'dispatched'.
    assert len(router.insert_calls["prompts"]) == 1
    assert router.insert_calls["prompts"][0]["status"] == "active"
    assert router.insert_calls["prompts"][0]["user_id"] == str(user_id)


def test_trigger_sends_no_push(
    router: TableRouter, fake_sb: MagicMock, stub_send_push: MagicMock
) -> None:
    user_id = uuid4()
    _stub_group(router)
    client = _build_client(fake_sb, dev_enabled=True)

    client.post(
        "/dev/prompts/trigger",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={"group_id": str(uuid4()), "media_type": "photo"},
    )
    stub_send_push.assert_not_called()


def test_trigger_video_target_length_in_range(
    router: TableRouter, fake_sb: MagicMock, stub_send_push: MagicMock
) -> None:
    user_id = uuid4()
    _stub_group(router)
    client = _build_client(fake_sb, dev_enabled=True)

    response = client.post(
        "/dev/prompts/trigger",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={"group_id": str(uuid4()), "media_type": "video"},
    )
    assert response.status_code == 200
    target = response.json()["target_video_length_seconds"]
    assert target is not None
    assert 3 <= target <= 10


def test_trigger_non_member_returns_404(
    router: TableRouter, fake_sb: MagicMock, stub_send_push: MagicMock
) -> None:
    user_id = uuid4()
    router.stub_select("group_members", None)
    client = _build_client(fake_sb, dev_enabled=True)

    response = client.post(
        "/dev/prompts/trigger",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={"group_id": str(uuid4()), "media_type": "photo"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Group not found"
