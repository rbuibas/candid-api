"""Unit tests for workers/generator.run_tick.

Pattern: a `TableRouter` fake routes `sb.table(name)` to per-table MagicMocks
so each table's chain returns its own data. The generator walks several
tables (groups, group_members, prompts) and needs them to differ.
"""

from datetime import UTC, datetime, time
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.workers import generator

BUCHAREST = "Europe/Bucharest"


def _group_row(
    *,
    prompts_per_day: int = 4,
    daily_window_start: str = "10:00:00",
    daily_window_end: str = "22:00:00",
    min_prompt_gap_minutes: int = 45,
    start_date: str = "2026-05-30",
    end_date: str = "2026-06-30",
    max_video_length_seconds: int = 10,
    group_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": group_id or str(uuid4()),
        "name": "test-group",
        "created_by": str(uuid4()),
        "start_date": start_date,
        "end_date": end_date,
        "prompts_per_day": prompts_per_day,
        "daily_window_start": daily_window_start,
        "daily_window_end": daily_window_end,
        "min_prompt_gap_minutes": min_prompt_gap_minutes,
        "response_window_seconds": 300,
        "late_window_seconds": 1800,
        "max_video_length_seconds": max_video_length_seconds,
        "view_delay_seconds": 0,
        "created_at": "2026-05-30T00:00:00+00:00",
        "updated_at": "2026-05-30T00:00:00+00:00",
    }


class TableRouter:
    """Routes sb.table(name) to a per-name MagicMock.

    Each per-table MagicMock has helpers attached for the chain leaves the
    generator uses: select_result (the .execute() result on the chain),
    insert_calls (captured insert payloads).
    """

    def __init__(self) -> None:
        self.tables: dict[str, MagicMock] = {}
        self.insert_calls: dict[str, list[list[dict]]] = {}

    def __call__(self, name: str) -> MagicMock:
        if name not in self.tables:
            self.tables[name] = MagicMock(name=f"table[{name}]")
            self.insert_calls[name] = []

            def _capture_insert(rows: list[dict], _name: str = name) -> MagicMock:
                self.insert_calls[_name].append(rows)
                insert_mock = MagicMock()
                insert_mock.execute.return_value = MagicMock(data=rows)
                return insert_mock

            self.tables[name].insert.side_effect = _capture_insert
        return self.tables[name]

    def stub_select_chain(self, name: str, data: list[dict]) -> None:
        """Set the .execute().data for whatever .select(...).<filters>... chain ends."""
        # Replace the .select chain leaf with a MagicMock that always resolves
        # to data, regardless of how many .eq/.lte/.gte calls intervene.
        result = MagicMock()
        result.data = data
        chain = MagicMock()
        chain.execute.return_value = result

        # Make EVERY attribute access on chain return chain itself, so any
        # number of filter calls collapses to the same execute().
        def _passthrough(*_a: Any, **_k: Any) -> MagicMock:
            return chain

        chain.eq.side_effect = _passthrough
        chain.lte.side_effect = _passthrough
        chain.gte.side_effect = _passthrough
        chain.is_.side_effect = _passthrough
        chain.not_.is_.side_effect = _passthrough
        chain.maybe_single.return_value = chain

        self.tables.setdefault(name, MagicMock(name=f"table[{name}]"))
        if name not in self.insert_calls:
            self.insert_calls[name] = []

            def _capture_insert(rows: list[dict], _name: str = name) -> MagicMock:
                self.insert_calls[_name].append(rows)
                insert_mock = MagicMock()
                insert_mock.execute.return_value = MagicMock(data=rows)
                return insert_mock

            self.tables[name].insert.side_effect = _capture_insert
        self.tables[name].select.return_value = chain


def _wire(router: TableRouter) -> MagicMock:
    sb = MagicMock()
    sb.table.side_effect = router
    return sb


# --- Tests -----------------------------------------------------------


def test_generator_inserts_prompts_per_day(rng_seeded: object = None) -> None:
    router = TableRouter()
    group = _group_row(prompts_per_day=4, min_prompt_gap_minutes=45)
    user_id = str(uuid4())

    router.stub_select_chain("groups", [group])
    router.stub_select_chain(
        "group_members", [{"user_id": user_id, "profiles": {"timezone": "UTC"}}]
    )
    router.stub_select_chain("prompts", [])
    sb = _wire(router)

    # Pin `now` early in the local window so all 4 slots fit comfortably.
    now = datetime(2026, 5, 30, 10, 0, tzinfo=UTC)
    counts = generator.run_tick(sb, now=now, rng=__import__("random").Random(42))

    assert counts["groups"] == 1
    assert counts["members"] == 1
    assert counts["prompts_inserted"] == 4
    inserted = router.insert_calls["prompts"][0]
    assert len(inserted) == 4
    # Every slot's scheduled_at is inside the 10:00–22:00 UTC window of 2026-05-30.
    for row in inserted:
        ts = datetime.fromisoformat(row["scheduled_at"])
        assert ts >= datetime(2026, 5, 30, 10, 0, tzinfo=UTC)
        assert ts <= datetime(2026, 5, 30, 22, 0, tzinfo=UTC)
        assert row["status"] == "scheduled"


def test_generator_is_idempotent_on_rerun() -> None:
    router = TableRouter()
    user_id = str(uuid4())
    group_id = str(uuid4())
    group = _group_row(prompts_per_day=4, group_id=group_id)

    # 4 existing scheduled prompts for the same local_date already there.
    existing = [
        {
            "scheduled_at": "2026-05-30T11:00:00+00:00",
            "status": "scheduled",
        }
        for _ in range(4)
    ]
    router.stub_select_chain("groups", [group])
    router.stub_select_chain(
        "group_members", [{"user_id": user_id, "profiles": {"timezone": "UTC"}}]
    )
    router.stub_select_chain("prompts", existing)
    sb = _wire(router)

    counts = generator.run_tick(sb, now=datetime(2026, 5, 30, 10, 0, tzinfo=UTC))
    assert counts["prompts_inserted"] == 0
    assert router.insert_calls["prompts"] == []


def test_generator_counts_missed_toward_total() -> None:
    """User-decision pinned by the plan: missed prompts consume slots."""
    router = TableRouter()
    user_id = str(uuid4())
    group = _group_row(prompts_per_day=4)
    existing = [{"scheduled_at": "2026-05-30T10:30:00+00:00", "status": "missed"}] * 4

    router.stub_select_chain("groups", [group])
    router.stub_select_chain(
        "group_members", [{"user_id": user_id, "profiles": {"timezone": "UTC"}}]
    )
    router.stub_select_chain("prompts", existing)
    sb = _wire(router)

    counts = generator.run_tick(sb, now=datetime(2026, 5, 30, 10, 0, tzinfo=UTC))
    assert counts["prompts_inserted"] == 0


def test_generator_respects_min_gap() -> None:
    router = TableRouter()
    user_id = str(uuid4())
    # 4 slots in a 6-hour window with 60-minute gap → all slots ≥3600s apart.
    group = _group_row(
        prompts_per_day=4,
        daily_window_start="10:00:00",
        daily_window_end="16:00:00",
        min_prompt_gap_minutes=60,
    )
    router.stub_select_chain("groups", [group])
    router.stub_select_chain(
        "group_members", [{"user_id": user_id, "profiles": {"timezone": "UTC"}}]
    )
    router.stub_select_chain("prompts", [])
    sb = _wire(router)

    counts = generator.run_tick(sb, now=datetime(2026, 5, 30, 10, 0, tzinfo=UTC))
    assert counts["prompts_inserted"] >= 1
    inserted = router.insert_calls["prompts"][0]
    times = sorted(datetime.fromisoformat(r["scheduled_at"]) for r in inserted)
    for a, b in zip(times, times[1:], strict=False):
        assert (b - a).total_seconds() >= 3600


def test_generator_skips_outside_group_date_range() -> None:
    router = TableRouter()
    # Group ended yesterday.
    group = _group_row(start_date="2026-05-01", end_date="2026-05-29")
    user_id = str(uuid4())

    # The lte/gte SELECT would normally exclude this group, but our chain stub
    # ignores filters — so the per-member loop's local_date check is what
    # protects us.
    router.stub_select_chain("groups", [group])
    router.stub_select_chain(
        "group_members", [{"user_id": user_id, "profiles": {"timezone": "UTC"}}]
    )
    router.stub_select_chain("prompts", [])
    sb = _wire(router)

    counts = generator.run_tick(sb, now=datetime(2026, 5, 30, 10, 0, tzinfo=UTC))
    assert counts["prompts_inserted"] == 0


def test_generator_uses_member_timezone() -> None:
    router = TableRouter()
    group = _group_row(
        daily_window_start="10:00:00",
        daily_window_end="22:00:00",
        prompts_per_day=2,
    )
    user_id = str(uuid4())
    router.stub_select_chain("groups", [group])
    router.stub_select_chain(
        "group_members",
        [{"user_id": user_id, "profiles": {"timezone": BUCHAREST}}],
    )
    router.stub_select_chain("prompts", [])
    sb = _wire(router)

    # now = 06:00 UTC = 09:00 Bucharest → window opens at 10:00 Bucharest = 07:00 UTC.
    now = datetime(2026, 5, 30, 6, 0, tzinfo=UTC)
    counts = generator.run_tick(sb, now=now, rng=__import__("random").Random(7))
    assert counts["prompts_inserted"] >= 1

    bucharest = ZoneInfo(BUCHAREST)
    for row in router.insert_calls["prompts"][0]:
        ts_local = datetime.fromisoformat(row["scheduled_at"]).astimezone(bucharest).time()
        assert time(10, 0) <= ts_local <= time(22, 0)


def test_generator_clips_to_24h_horizon_without_error() -> None:
    router = TableRouter()
    # `now` is 30 minutes before window end → barely any room for 4 slots.
    group = _group_row(
        prompts_per_day=4,
        daily_window_start="10:00:00",
        daily_window_end="22:00:00",
        min_prompt_gap_minutes=45,
    )
    user_id = str(uuid4())
    router.stub_select_chain("groups", [group])
    router.stub_select_chain(
        "group_members", [{"user_id": user_id, "profiles": {"timezone": "UTC"}}]
    )
    router.stub_select_chain("prompts", [])
    sb = _wire(router)

    counts = generator.run_tick(sb, now=datetime(2026, 5, 30, 21, 30, tzinfo=UTC))
    # 30 min before today's window ends; the 24h horizon reaches into
    # tomorrow's window too. Up to prompts_per_day per local_date for both
    # days; the smoke test is "no crash, sane count <= 8."
    assert 0 <= counts["prompts_inserted"] <= 8


def test_generator_seeded_rng_produces_stable_slots() -> None:
    import random

    router1 = TableRouter()
    router2 = TableRouter()
    user_id = str(uuid4())
    group_id = str(uuid4())
    group = _group_row(prompts_per_day=4, group_id=group_id)
    for r in (router1, router2):
        r.stub_select_chain("groups", [group])
        r.stub_select_chain(
            "group_members", [{"user_id": user_id, "profiles": {"timezone": "UTC"}}]
        )
        r.stub_select_chain("prompts", [])

    now = datetime(2026, 5, 30, 10, 0, tzinfo=UTC)
    generator.run_tick(_wire(router1), now=now, rng=random.Random(99))
    generator.run_tick(_wire(router2), now=now, rng=random.Random(99))

    a = [r["scheduled_at"] for r in router1.insert_calls["prompts"][0]]
    b = [r["scheduled_at"] for r in router2.insert_calls["prompts"][0]]
    assert a == b


def test_generator_video_target_length_in_range() -> None:
    import random

    router = TableRouter()
    user_id = str(uuid4())
    group = _group_row(prompts_per_day=4, max_video_length_seconds=10)
    router.stub_select_chain("groups", [group])
    router.stub_select_chain(
        "group_members", [{"user_id": user_id, "profiles": {"timezone": "UTC"}}]
    )
    router.stub_select_chain("prompts", [])
    sb = _wire(router)

    now = datetime(2026, 5, 30, 10, 0, tzinfo=UTC)
    generator.run_tick(sb, now=now, rng=random.Random(1))

    for row in router.insert_calls["prompts"][0]:
        if row["media_type"] == "video":
            assert row["target_video_length_seconds"] is not None
            assert 3 <= row["target_video_length_seconds"] <= 10
        else:
            assert row["target_video_length_seconds"] is None


# --- Pure-helper tests for the slotting math ------------------------


def test_bucket_jitter_returns_empty_for_no_ranges() -> None:
    rng = __import__("random").Random(1)
    assert generator._bucket_jitter_slots([], 3, 60, [], rng) == []


def test_bucket_jitter_respects_existing_prompts() -> None:
    import random

    # Single hour-long range; existing slot at minute 0; gap = 30 min.
    start = datetime(2026, 5, 30, 10, 0, tzinfo=UTC)
    end = datetime(2026, 5, 30, 11, 0, tzinfo=UTC)
    existing = [start]  # blocks the first 30 min
    slots = generator._bucket_jitter_slots(
        [(start, end)],
        needed=1,
        min_gap_seconds=30 * 60,
        existing_utc=existing,
        rng=random.Random(1),
    )
    assert len(slots) == 1
    assert (slots[0] - start).total_seconds() >= 30 * 60
