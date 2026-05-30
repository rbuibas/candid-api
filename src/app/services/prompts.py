"""Prompts service — pure timing helpers (Phase 4 Commit 5).

This module starts as pure helpers around timezones and deadlines so the
generator/dispatcher/expirer + the /prompts router can all share the same
math. DB calls (get_active_for_user, get_for_user, fire_prompt_now) land in
later commits in this same file.

Window-wrap is the easiest place to get cute and wrong: when daily_window_end
< daily_window_start the window crosses midnight, producing two UTC ranges
for one local "day."
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.models.prompt import PromptUIState


def local_window_to_utc_ranges(
    local_date: date,
    window_start: time,
    window_end: time,
    tz: str,
) -> list[tuple[datetime, datetime]]:
    """Convert a per-user daily window for one local date into UTC ranges.

    - end == start → []. The group operator picked a zero-length window.
    - end > start → one UTC range, `[start, end]` on `local_date` in `tz`.
    - end < start → two UTC ranges spanning midnight:
        [start, midnight) on `local_date`
        [midnight, end) on `local_date + 1`
      Both localized then converted to UTC.

    Using zoneinfo handles DST transitions; the spring-forward day yields a
    shorter window naturally because the localized datetimes carry the
    correct offset.
    """
    if window_end == window_start:
        return []

    zone = ZoneInfo(tz)

    if window_end > window_start:
        start_local = datetime.combine(local_date, window_start, tzinfo=zone)
        end_local = datetime.combine(local_date, window_end, tzinfo=zone)
        return [(start_local.astimezone(UTC), end_local.astimezone(UTC))]

    # Window crosses midnight.
    start_local = datetime.combine(local_date, window_start, tzinfo=zone)
    midnight_next = datetime.combine(local_date + timedelta(days=1), time(0, 0), tzinfo=zone)
    end_local_next = datetime.combine(local_date + timedelta(days=1), window_end, tzinfo=zone)
    return [
        (start_local.astimezone(UTC), midnight_next.astimezone(UTC)),
        (midnight_next.astimezone(UTC), end_local_next.astimezone(UTC)),
    ]


def compute_deadlines(
    dispatched_at: datetime,
    response_window_seconds: int,
    late_window_seconds: int,
) -> tuple[datetime, datetime]:
    """Return (on_time_deadline, late_deadline) anchored on dispatched_at.

    on_time_deadline = dispatched_at + response_window_seconds
    late_deadline    = on_time_deadline + late_window_seconds
    """
    on_time = dispatched_at + timedelta(seconds=response_window_seconds)
    late = on_time + timedelta(seconds=late_window_seconds)
    return on_time, late


def compute_state(
    now: datetime,
    dispatched_at: datetime,
    response_window_seconds: int,
    late_window_seconds: int,
) -> PromptUIState:
    """Pick the UI state the client should render right now.

    Inclusive boundaries on the early side: a confirm landing exactly at
    on_time_deadline is still on-time; a read at the same moment renders as
    ACTIVE. Past late_deadline is MISSED.
    """
    on_time, late = compute_deadlines(dispatched_at, response_window_seconds, late_window_seconds)
    if now <= on_time:
        return PromptUIState.ACTIVE
    if now <= late:
        return PromptUIState.LATE
    return PromptUIState.MISSED
