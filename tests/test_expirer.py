"""Unit tests for workers/expirer.run_tick."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

from app.workers import expirer


def _row(*, dispatched_offset_seconds: int, rws: int = 300, lws: int = 1800) -> dict[str, Any]:
    base = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)
    dispatched = base + timedelta(seconds=dispatched_offset_seconds)
    return {
        "id": str(uuid4()),
        "dispatched_at": dispatched.isoformat(),
        "groups": {"response_window_seconds": rws, "late_window_seconds": lws},
    }


class TableRouter:
    def __init__(self) -> None:
        self.tables: dict[str, MagicMock] = {}
        self.select_data: dict[str, list[dict]] = {}
        self.update_calls: list[tuple[dict, tuple]] = []

    def __call__(self, name: str) -> MagicMock:
        if name not in self.tables:
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
            result = MagicMock()
            result.data = self.select_data.get(name, [])
            chain.execute.return_value = result
            t._select_result = result

            router = self

            def _capture_update(payload: dict) -> MagicMock:
                inner = MagicMock()

                def _capture_in(col: str, vals: list) -> MagicMock:
                    in_inner = MagicMock()

                    def _capture_execute() -> MagicMock:
                        router.update_calls.append((payload, (col, list(vals))))
                        return MagicMock(data=[])

                    in_inner.execute.side_effect = _capture_execute
                    return in_inner

                inner.in_.side_effect = _capture_in
                return inner

            t.update.side_effect = _capture_update
            self.tables[name] = t
        return self.tables[name]

    def stub_select(self, name: str, data: list[dict]) -> None:
        self.select_data[name] = data
        t = self.__call__(name)
        t._select_result.data = data


def _wire(router: TableRouter) -> MagicMock:
    sb = MagicMock()
    sb.table.side_effect = router
    return sb


# --- Tests --------------------------------------------------------


def test_expirer_marks_past_late_deadline_as_missed() -> None:
    """rws+lws = 2100s. dispatched_at = 12:00; now = 13:00 → 3600s past, missed."""
    router = TableRouter()
    expired = _row(dispatched_offset_seconds=0)
    router.stub_select("prompts", [expired])
    sb = _wire(router)

    counts = expirer.run_tick(sb, now=datetime(2026, 5, 30, 13, 0, tzinfo=UTC))
    assert counts == {"prompts_missed": 1}
    assert len(router.update_calls) == 1
    payload, (col, vals) = router.update_calls[0]
    assert payload == {"status": "missed"}
    assert col == "id"
    assert vals == [expired["id"]]


def test_expirer_leaves_in_window_rows_alone() -> None:
    router = TableRouter()
    # dispatched 60s ago; rws=300; lws=1800 → 30min+5s left.
    fresh = _row(dispatched_offset_seconds=-60)
    fresh["dispatched_at"] = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    router.stub_select("prompts", [fresh])
    sb = _wire(router)

    counts = expirer.run_tick(sb, now=datetime.now(UTC))
    assert counts == {"prompts_missed": 0}
    assert router.update_calls == []


def test_expirer_empty_set_returns_zero() -> None:
    router = TableRouter()
    router.stub_select("prompts", [])
    sb = _wire(router)
    counts = expirer.run_tick(sb, now=datetime(2026, 5, 30, 12, 0, tzinfo=UTC))
    assert counts == {"prompts_missed": 0}


def test_expirer_batches_multiple_expired_in_single_update() -> None:
    router = TableRouter()
    rows = [_row(dispatched_offset_seconds=0) for _ in range(3)]
    router.stub_select("prompts", rows)
    sb = _wire(router)

    expirer.run_tick(sb, now=datetime(2026, 5, 30, 13, 0, tzinfo=UTC))
    assert len(router.update_calls) == 1
    payload, (col, vals) = router.update_calls[0]
    assert payload == {"status": "missed"}
    assert sorted(vals) == sorted(r["id"] for r in rows)


def test_expirer_mixes_keep_and_expire_correctly() -> None:
    router = TableRouter()
    keep = _row(dispatched_offset_seconds=0)
    keep["dispatched_at"] = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    expired = _row(dispatched_offset_seconds=0)
    expired["dispatched_at"] = (datetime.now(UTC) - timedelta(seconds=5000)).isoformat()
    router.stub_select("prompts", [keep, expired])
    sb = _wire(router)

    expirer.run_tick(sb, now=datetime.now(UTC))
    assert len(router.update_calls) == 1
    _, (col, vals) = router.update_calls[0]
    assert vals == [expired["id"]]
