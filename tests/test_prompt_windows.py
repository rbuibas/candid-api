"""Unit tests for services/prompts.py timing helpers.

Pure functions only — no DB, no FastAPI, no Supabase mocks needed.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.models.prompt import PromptUIState
from app.services.prompts import (
    compute_deadlines,
    compute_state,
    local_window_to_utc_ranges,
)

# Bachelor party MVP is in Bucharest; pick that as the realistic tz.
BUCHAREST = "Europe/Bucharest"


# --- local_window_to_utc_ranges ---------------------------------------


def test_non_wrap_window_yields_single_utc_range() -> None:
    # 2026-05-30 is mid-summer; Bucharest is UTC+3.
    ranges = local_window_to_utc_ranges(date(2026, 5, 30), time(10, 0), time(22, 0), BUCHAREST)
    assert len(ranges) == 1
    start, end = ranges[0]
    assert start == datetime(2026, 5, 30, 7, 0, tzinfo=UTC)
    assert end == datetime(2026, 5, 30, 19, 0, tzinfo=UTC)
    assert end - start == timedelta(hours=12)


def test_wrap_window_yields_two_utc_ranges_across_midnight() -> None:
    # Window 22:00 → 02:00 Bucharest, on local date 2026-05-30.
    ranges = local_window_to_utc_ranges(date(2026, 5, 30), time(22, 0), time(2, 0), BUCHAREST)
    assert len(ranges) == 2

    first_start, first_end = ranges[0]
    second_start, second_end = ranges[1]

    # First range: 22:00 Bucharest on 2026-05-30 → midnight Bucharest on 2026-05-31.
    assert first_start == datetime(2026, 5, 30, 19, 0, tzinfo=UTC)
    assert first_end == datetime(2026, 5, 30, 21, 0, tzinfo=UTC)
    # Second range: midnight → 02:00 Bucharest on 2026-05-31.
    assert second_start == first_end
    assert second_end == datetime(2026, 5, 30, 23, 0, tzinfo=UTC)

    total = (first_end - first_start) + (second_end - second_start)
    assert total == timedelta(hours=4)


def test_end_equals_start_returns_empty_window() -> None:
    assert local_window_to_utc_ranges(date(2026, 5, 30), time(10, 0), time(10, 0), "UTC") == []


def test_utc_tz_passes_through_without_offset() -> None:
    ranges = local_window_to_utc_ranges(date(2026, 5, 30), time(10, 0), time(22, 0), "UTC")
    assert ranges == [
        (
            datetime(2026, 5, 30, 10, 0, tzinfo=UTC),
            datetime(2026, 5, 30, 22, 0, tzinfo=UTC),
        )
    ]


def test_dst_spring_forward_yields_shorter_window() -> None:
    """On the spring-forward day, a 00:00–06:00 local window is 5 hours, not 6.

    Bucharest DST 2026 begins 2026-03-29 at 03:00 → 04:00 local time.
    If zoneinfo data is unavailable (e.g. base Windows install without
    tzdata), the test is skipped.
    """
    try:
        ZoneInfo(BUCHAREST)
    except Exception:  # pragma: no cover — environment-dependent
        pytest.skip("Europe/Bucharest tzdata not available")

    ranges = local_window_to_utc_ranges(date(2026, 3, 29), time(0, 0), time(6, 0), BUCHAREST)
    assert len(ranges) == 1
    start, end = ranges[0]
    assert end - start == timedelta(hours=5)


# --- compute_deadlines ------------------------------------------------


def test_compute_deadlines_arithmetic() -> None:
    dispatched = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)
    on_time, late = compute_deadlines(dispatched, 300, 1800)
    assert on_time == dispatched + timedelta(seconds=300)
    assert late == dispatched + timedelta(seconds=2100)


# --- compute_state ----------------------------------------------------


@pytest.fixture
def dispatched() -> datetime:
    return datetime(2026, 5, 30, 12, 0, tzinfo=UTC)


def test_state_at_dispatched_is_active(dispatched: datetime) -> None:
    assert compute_state(dispatched, dispatched, 300, 1800) is PromptUIState.ACTIVE


def test_state_at_on_time_deadline_is_active(dispatched: datetime) -> None:
    now = dispatched + timedelta(seconds=300)
    assert compute_state(now, dispatched, 300, 1800) is PromptUIState.ACTIVE


def test_state_one_second_after_on_time_is_late(dispatched: datetime) -> None:
    now = dispatched + timedelta(seconds=301)
    assert compute_state(now, dispatched, 300, 1800) is PromptUIState.LATE


def test_state_at_late_deadline_is_late(dispatched: datetime) -> None:
    now = dispatched + timedelta(seconds=2100)
    assert compute_state(now, dispatched, 300, 1800) is PromptUIState.LATE


def test_state_one_second_after_late_is_missed(dispatched: datetime) -> None:
    now = dispatched + timedelta(seconds=2101)
    assert compute_state(now, dispatched, 300, 1800) is PromptUIState.MISSED
