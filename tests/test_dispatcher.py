"""Unit tests for workers/dispatcher.run_tick."""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.clients.firebase import SendResult
from app.workers import dispatcher


def _prompt(
    *,
    user_id: str,
    group_id: str,
    media_type: str = "photo",
    target_video_length_seconds: int | None = None,
) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "group_id": group_id,
        "user_id": user_id,
        "scheduled_at": "2026-05-30T10:00:00+00:00",
        "dispatched_at": None,
        "local_date": "2026-05-30",
        "media_type": media_type,
        "target_video_length_seconds": target_video_length_seconds,
        "status": "scheduled",
        "created_at": "2026-05-30T00:00:00+00:00",
    }


class TableRouter:
    """Per-table MagicMock router.

    Tracks insert/update/delete/select calls per table so tests can assert
    on what the dispatcher actually did.
    """

    def __init__(self) -> None:
        self.tables: dict[str, MagicMock] = {}
        self.select_data: dict[str, list[dict] | dict | None] = {}
        self.update_calls: list[tuple[str, dict, tuple]] = []
        self.delete_calls: list[tuple[str, str, list]] = []

    def __call__(self, name: str) -> MagicMock:
        if name not in self.tables:
            self.tables[name] = self._build(name)
        return self.tables[name]

    def _build(self, name: str) -> MagicMock:
        t = MagicMock(name=f"table[{name}]")
        router = self

        # SELECT chain: collapses any number of filter calls into one execute.
        select_chain = MagicMock()

        def _select(*_a: Any, **_k: Any) -> MagicMock:
            return select_chain

        result = MagicMock()
        select_chain.execute.return_value = result

        def _data_getter() -> list[dict] | dict | None:
            return router.select_data.get(name, [])

        # Patch result.data via a property-like attribute that re-reads
        # router.select_data on every access — so tests can update it
        # between rows in side_effects if needed.
        # Simpler: just set it at call time. We'll set it via stub_select.
        t.select.side_effect = _select
        select_chain.eq.side_effect = _select
        select_chain.lte.side_effect = _select
        select_chain.gte.side_effect = _select
        select_chain.is_.side_effect = _select
        select_chain.not_.is_.side_effect = _select
        select_chain.maybe_single.return_value = select_chain
        t._select_result = result  # exposed for stub_select

        # UPDATE chain: .update(payload).eq(col, val).execute()
        update_chain = MagicMock()

        def _capture_update(payload: dict) -> MagicMock:
            inner = MagicMock()

            def _capture_eq(col: str, val: Any) -> MagicMock:
                eq_inner = MagicMock()

                def _capture_execute() -> MagicMock:
                    router.update_calls.append((name, payload, (col, val)))
                    return MagicMock(data=[])

                eq_inner.execute.side_effect = _capture_execute
                return eq_inner

            inner.eq.side_effect = _capture_eq
            return inner

        t.update.side_effect = _capture_update
        del update_chain

        # DELETE chain: .delete().in_(col, vals).execute()
        def _capture_delete() -> MagicMock:
            inner = MagicMock()

            def _capture_in(col: str, vals: list) -> MagicMock:
                in_inner = MagicMock()

                def _capture_execute() -> MagicMock:
                    router.delete_calls.append((name, col, list(vals)))
                    return MagicMock(data=[])

                in_inner.execute.side_effect = _capture_execute
                return in_inner

            inner.in_.side_effect = _capture_in
            return inner

        t.delete.side_effect = _capture_delete
        return t

    def stub_select(self, name: str, data: list[dict] | dict | None) -> None:
        self.select_data[name] = data
        # Bind the data to the existing select chain (or build the table now).
        t = self.__call__(name)
        t._select_result.data = data


def _wire(router: TableRouter) -> MagicMock:
    sb = MagicMock()
    sb.table.side_effect = router
    return sb


@pytest.fixture
def stub_send_push(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    m = MagicMock(return_value=SendResult(0, 0, []))
    monkeypatch.setattr("app.workers.dispatcher.firebase.send_push", m)
    return m


# --- Selection -------------------------------------------------------


def test_dispatcher_selects_due_scheduled_prompts(
    stub_send_push: MagicMock,
) -> None:
    router = TableRouter()
    user_id = str(uuid4())
    group_id = str(uuid4())
    prompt = _prompt(user_id=user_id, group_id=group_id)
    router.stub_select("prompts", [prompt])
    router.stub_select("groups", {"response_window_seconds": 300, "late_window_seconds": 1800})
    router.stub_select("devices", [{"fcm_token": "tok-1"}])

    sb = _wire(router)
    counts = dispatcher.run_tick(sb, now=datetime(2026, 5, 30, 11, 0, tzinfo=UTC))

    assert counts["prompts_dispatched"] == 1
    assert counts["tokens_sent"] == 0  # send_push default mock returns 0


def test_dispatcher_no_due_prompts_returns_zero(stub_send_push: MagicMock) -> None:
    router = TableRouter()
    router.stub_select("prompts", [])
    sb = _wire(router)
    counts = dispatcher.run_tick(sb, now=datetime(2026, 5, 30, 11, 0, tzinfo=UTC))
    assert counts == {
        "prompts_dispatched": 0,
        "tokens_sent": 0,
        "devices_pruned": 0,
        "prompts_cancelled_locked": 0,
    }
    stub_send_push.assert_not_called()


# --- Push payload + status flip ----------------------------------


def test_dispatcher_sends_push_with_correct_title_and_data(
    stub_send_push: MagicMock,
) -> None:
    router = TableRouter()
    user_id = str(uuid4())
    group_id = str(uuid4())
    prompt = _prompt(user_id=user_id, group_id=group_id, media_type="photo")
    router.stub_select("prompts", [prompt])
    router.stub_select("groups", {"response_window_seconds": 300, "late_window_seconds": 1800})
    router.stub_select("devices", [{"fcm_token": "tok-1"}, {"fcm_token": "tok-2"}])
    stub_send_push.return_value = SendResult(2, 0, [])
    sb = _wire(router)

    now = datetime(2026, 5, 30, 11, 0, tzinfo=UTC)
    dispatcher.run_tick(sb, now=now)

    call = stub_send_push.call_args
    assert call.args[0] == ["tok-1", "tok-2"]
    data = call.args[1]
    assert data["prompt_id"] == prompt["id"]
    assert data["group_id"] == group_id
    assert data["media_type"] == "photo"
    assert data["response_window_seconds"] == 300
    assert data["late_window_seconds"] == 1800
    assert data["dispatched_at"] == now.isoformat()
    assert call.kwargs["title"] == "Time to capture"
    assert call.kwargs["body"] == ""
    assert "target_video_length_seconds" not in data


def test_dispatcher_includes_target_video_length_for_video(
    stub_send_push: MagicMock,
) -> None:
    router = TableRouter()
    user_id = str(uuid4())
    group_id = str(uuid4())
    prompt = _prompt(
        user_id=user_id, group_id=group_id, media_type="video", target_video_length_seconds=7
    )
    router.stub_select("prompts", [prompt])
    router.stub_select("groups", {"response_window_seconds": 300, "late_window_seconds": 1800})
    router.stub_select("devices", [{"fcm_token": "tok"}])
    stub_send_push.return_value = SendResult(1, 0, [])
    sb = _wire(router)

    dispatcher.run_tick(sb, now=datetime(2026, 5, 30, 11, 0, tzinfo=UTC))
    data = stub_send_push.call_args.args[1]
    assert data["target_video_length_seconds"] == 7


def test_dispatcher_flips_status_and_writes_dispatched_at(
    stub_send_push: MagicMock,
) -> None:
    router = TableRouter()
    user_id = str(uuid4())
    group_id = str(uuid4())
    prompt = _prompt(user_id=user_id, group_id=group_id)
    router.stub_select("prompts", [prompt])
    router.stub_select("groups", {"response_window_seconds": 300, "late_window_seconds": 1800})
    router.stub_select("devices", [{"fcm_token": "tok"}])
    sb = _wire(router)

    now = datetime(2026, 5, 30, 11, 0, tzinfo=UTC)
    dispatcher.run_tick(sb, now=now)

    prompt_updates = [u for u in router.update_calls if u[0] == "prompts"]
    assert len(prompt_updates) == 1
    table, payload, (col, val) = prompt_updates[0]
    assert payload == {"status": "active", "dispatched_at": now.isoformat()}
    assert (col, val) == ("id", prompt["id"])


def test_dispatcher_flips_status_even_without_devices(
    stub_send_push: MagicMock,
) -> None:
    router = TableRouter()
    user_id = str(uuid4())
    group_id = str(uuid4())
    prompt = _prompt(user_id=user_id, group_id=group_id)
    router.stub_select("prompts", [prompt])
    router.stub_select("groups", {"response_window_seconds": 300, "late_window_seconds": 1800})
    router.stub_select("devices", [])
    sb = _wire(router)

    counts = dispatcher.run_tick(sb, now=datetime(2026, 5, 30, 11, 0, tzinfo=UTC))
    assert counts["prompts_dispatched"] == 1
    stub_send_push.assert_not_called()
    # Still flipped status:
    assert any(u[0] == "prompts" for u in router.update_calls)


# --- Invalid-token cleanup ---------------------------------------


def test_dispatcher_deletes_invalid_tokens(stub_send_push: MagicMock) -> None:
    router = TableRouter()
    user_id = str(uuid4())
    group_id = str(uuid4())
    prompt = _prompt(user_id=user_id, group_id=group_id)
    router.stub_select("prompts", [prompt])
    router.stub_select("groups", {"response_window_seconds": 300, "late_window_seconds": 1800})
    router.stub_select("devices", [{"fcm_token": "live"}, {"fcm_token": "dead"}])
    stub_send_push.return_value = SendResult(1, 1, ["dead"])
    sb = _wire(router)

    counts = dispatcher.run_tick(sb, now=datetime(2026, 5, 30, 11, 0, tzinfo=UTC))
    assert counts["devices_pruned"] == 1
    device_deletes = [d for d in router.delete_calls if d[0] == "devices"]
    assert len(device_deletes) == 1
    assert device_deletes[0] == ("devices", "fcm_token", ["dead"])


# --- Group cache --------------------------------------------------


def test_dispatcher_caches_group_settings_within_tick(
    stub_send_push: MagicMock,
) -> None:
    router = TableRouter()
    user_id = str(uuid4())
    group_id = str(uuid4())
    prompts = [_prompt(user_id=user_id, group_id=group_id) for _ in range(3)]
    router.stub_select("prompts", prompts)
    router.stub_select("groups", {"response_window_seconds": 300, "late_window_seconds": 1800})
    router.stub_select("devices", [{"fcm_token": "tok"}])
    stub_send_push.return_value = SendResult(1, 0, [])
    sb = _wire(router)

    dispatcher.run_tick(sb, now=datetime(2026, 5, 30, 11, 0, tzinfo=UTC))

    # Only ONE select on `groups` despite three prompts in the batch.
    groups_table = router.tables["groups"]
    assert groups_table.select.call_count == 1


# --- Phase 6: lock re-check --------------------------------------


def test_dispatcher_cancels_prompt_when_group_locked(stub_send_push: MagicMock) -> None:
    """A prompt scheduled while active but dispatched after the group locks is
    cancelled to a terminal state (missed), not pushed."""
    router = TableRouter()
    user_id = str(uuid4())
    group_id = str(uuid4())
    prompt = _prompt(user_id=user_id, group_id=group_id)
    router.stub_select("prompts", [prompt])
    # end_date well in the real past → locked (compute_lifecycle uses real now).
    router.stub_select(
        "groups",
        {
            "response_window_seconds": 300,
            "late_window_seconds": 1800,
            "start_date": "2024-01-01",
            "end_date": "2024-06-01",
        },
    )
    router.stub_select("devices", [{"fcm_token": "tok"}])
    sb = _wire(router)

    counts = dispatcher.run_tick(sb, now=datetime(2026, 5, 30, 11, 0, tzinfo=UTC))

    assert counts["prompts_cancelled_locked"] == 1
    assert counts["prompts_dispatched"] == 0
    # No push for a cancelled prompt.
    stub_send_push.assert_not_called()

    # Flipped to a terminal status='missed'; no dispatched_at written.
    prompt_updates = [u for u in router.update_calls if u[0] == "prompts"]
    assert len(prompt_updates) == 1
    _table, payload, (col, val) = prompt_updates[0]
    assert payload == {"status": "missed"}
    assert (col, val) == ("id", prompt["id"])


def test_dispatcher_dispatches_when_group_active_with_dates(
    stub_send_push: MagicMock,
) -> None:
    """Lifecycle dates present but the group is still active → normal dispatch."""
    router = TableRouter()
    user_id = str(uuid4())
    group_id = str(uuid4())
    prompt = _prompt(user_id=user_id, group_id=group_id)
    router.stub_select("prompts", [prompt])
    router.stub_select(
        "groups",
        {
            "response_window_seconds": 300,
            "late_window_seconds": 1800,
            "start_date": "2024-01-01",
            "end_date": "2099-12-31",
        },
    )
    router.stub_select("devices", [{"fcm_token": "tok"}])
    stub_send_push.return_value = SendResult(1, 0, [])
    sb = _wire(router)

    counts = dispatcher.run_tick(sb, now=datetime(2026, 5, 30, 11, 0, tzinfo=UTC))

    assert counts["prompts_cancelled_locked"] == 0
    assert counts["prompts_dispatched"] == 1
    # Active-flip happened (status='active', dispatched_at set), not a cancel.
    prompt_updates = [u for u in router.update_calls if u[0] == "prompts"]
    assert len(prompt_updates) == 1
    _table, payload, _eq = prompt_updates[0]
    assert payload["status"] == "active"
    assert "dispatched_at" in payload
