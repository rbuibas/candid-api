"""Prompt generator — hourly + once at worker startup.

For every active group (today between start_date and end_date), for each
member, ensure each local date touched by the rolling 24h horizon has
exactly `prompts_per_day` prompts scheduled. Respects
`min_prompt_gap_minutes`, the per-user `profiles.timezone`, and the
wrap-past-midnight `daily_window_*` shape.

Counting includes `missed` prompts (see plan): a user who silences
notifications still consumes one of their daily slots — the generator
does NOT top up missed prompts. This avoids late-day spam after a quiet
morning.

Idempotency: re-running within the same hour finds the count already at
target and inserts nothing.

Slotting algorithm — bucket-jitter. Partition the available UTC seconds
into K equal buckets, pick a random offset within each bucket, then
enforce `min_prompt_gap_minutes` with a left-shift clamp against both
previously-placed slots in this run AND the existing prompts already in
the row. Robust under tight gaps and reproducible from a seeded RNG.
"""

from __future__ import annotations

import logging
import random
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from supabase import Client

from app.clients.supabase import get_supabase
from app.services.prompts import local_window_to_utc_ranges

log = logging.getLogger(__name__)

# Statuses that "consume" a daily slot (everything except scheduled→missed
# happens server-side post-dispatch; all five contribute to the day's count).
_STATUSES_COUNTING_TOWARD_DAILY = ("scheduled", "active", "responded", "late", "missed")

_HORIZON = timedelta(hours=24)


def _parse_time(raw: Any) -> time:
    """Parse Postgres TIME ('HH:MM:SS' or 'HH:MM') into a time."""
    if isinstance(raw, time):
        return raw
    s = str(raw)
    parts = s.split(":")
    h, m = int(parts[0]), int(parts[1])
    sec = int(parts[2]) if len(parts) > 2 else 0
    return time(h, m, sec)


def _parse_date(raw: Any) -> date:
    if isinstance(raw, date):
        return raw
    return date.fromisoformat(str(raw))


def _parse_dt(raw: str) -> datetime:
    return datetime.fromisoformat(raw).astimezone(UTC)


def _local_dates_in_range(now: datetime, horizon: datetime, tz: str) -> list[date]:
    """All local dates whose midnight bound falls in [now, horizon].

    We don't try to be clever about wrap-past-midnight windows that bleed
    from the previous local date into the horizon — the per-date window
    helper already handles wraps that bleed INTO local_date + 1, and the
    only case we'd miss is a wrap that started before `now` and is still
    going. The dispatcher's per-minute cadence makes that a one-tick lag
    we can live with at MVP scale.
    """
    zone = ZoneInfo(tz)
    start_local = now.astimezone(zone).date()
    end_local = horizon.astimezone(zone).date()
    out: list[date] = []
    d = start_local
    while d <= end_local:
        out.append(d)
        d += timedelta(days=1)
    return out


def _clip_ranges(
    ranges: list[tuple[datetime, datetime]],
    floor: datetime,
    ceiling: datetime,
) -> list[tuple[datetime, datetime]]:
    """Intersect each (start, end) with [floor, ceiling]; drop empties."""
    out: list[tuple[datetime, datetime]] = []
    for s, e in ranges:
        cs = max(s, floor)
        ce = min(e, ceiling)
        if ce > cs:
            out.append((cs, ce))
    return out


def _offset_to_datetime(ranges: list[tuple[datetime, datetime]], offset_seconds: float) -> datetime:
    """Walk the (sorted) UTC ranges and convert an in-total offset into a UTC datetime."""
    remaining = offset_seconds
    for s, e in ranges:
        length = (e - s).total_seconds()
        if remaining <= length:
            return s + timedelta(seconds=remaining)
        remaining -= length
    # Past the end of available time → clamp to the last range's end.
    last = ranges[-1]
    return last[1]


def _bucket_jitter_slots(
    ranges: list[tuple[datetime, datetime]],
    needed: int,
    min_gap_seconds: int,
    existing_utc: list[datetime],
    rng: random.Random,
) -> list[datetime]:
    """Pick `needed` new UTC datetimes inside `ranges` respecting min_gap.

    Algorithm:
    1. Partition total available seconds into `needed` equal buckets.
    2. Sample one random offset within each bucket.
    3. Walk left-to-right, left-shift-clamping each candidate so it sits at
       least `min_gap_seconds` after the previous placed slot (existing OR
       new). If the clamp would push past the available end, drop it.
    """
    if not ranges or needed <= 0:
        return []
    total = sum((e - s).total_seconds() for s, e in ranges)
    if total <= 0:
        return []
    bucket = total / needed

    candidates: list[float] = []
    for i in range(needed):
        b_start = i * bucket
        b_end = (i + 1) * bucket
        candidates.append(rng.uniform(b_start, b_end))

    # Existing prompts that fall inside ranges contribute to the gap check.
    # Anything outside is irrelevant — it can't conflict.
    inside_existing = sorted(t for t in existing_utc if _is_in_ranges(t, ranges))

    placed: list[datetime] = []
    floor = None  # earliest a new slot may land
    # Walk candidates AND existing slots in time order. We track `floor` =
    # max(previous placed/existing) + gap; if next candidate < floor, push it.
    candidate_dts = [_offset_to_datetime(ranges, c) for c in candidates]

    merged_pre = sorted(inside_existing)
    pre_idx = 0
    for cand in candidate_dts:
        # Advance `floor` over any pre-existing slots that landed before cand.
        while pre_idx < len(merged_pre) and merged_pre[pre_idx] <= cand:
            floor = _max_dt(floor, merged_pre[pre_idx] + timedelta(seconds=min_gap_seconds))
            pre_idx += 1
        if floor is not None and cand < floor:
            cand = floor
        if not _is_in_ranges(cand, ranges):
            # Pushed past the available end; drop this candidate.
            continue
        placed.append(cand)
        floor = cand + timedelta(seconds=min_gap_seconds)
    # Drain remaining pre-existing slots into the floor (no new effect).
    return placed


def _max_dt(a: datetime | None, b: datetime) -> datetime:
    return b if a is None else max(a, b)


def _is_in_ranges(t: datetime, ranges: list[tuple[datetime, datetime]]) -> bool:
    for s, e in ranges:
        if s <= t <= e:
            return True
    return False


def _build_prompt_row(
    group: dict,
    user_id: str,
    local_date: date,
    scheduled_at: datetime,
    rng: random.Random,
) -> dict:
    media_type = rng.choice(["photo", "video"])
    target_video: int | None = None
    if media_type == "video":
        max_video = int(group["max_video_length_seconds"])
        target_video = rng.randint(3, max_video) if max_video >= 3 else max_video
    return {
        "id": str(uuid4()),
        "group_id": group["id"],
        "user_id": user_id,
        "scheduled_at": scheduled_at.isoformat(),
        "local_date": local_date.isoformat(),
        "media_type": media_type,
        "target_video_length_seconds": target_video,
        "status": "scheduled",
    }


def run_tick(
    sb: Client,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> dict[str, int]:
    """Generate the next 24h of prompts. Idempotent within the hour."""
    now = now or datetime.now(UTC)
    rng = rng or random.Random()
    horizon = now + _HORIZON
    today_utc = now.date()

    counts = {"groups": 0, "members": 0, "prompts_inserted": 0}

    groups_result = (
        sb.table("groups")
        .select("*")
        .lte("start_date", (today_utc + timedelta(days=1)).isoformat())
        .gte("end_date", (today_utc - timedelta(days=1)).isoformat())
        .execute()
    )
    groups = groups_result.data or []

    for group in groups:
        counts["groups"] += 1
        group_start = _parse_date(group["start_date"])
        group_end = _parse_date(group["end_date"])
        window_start = _parse_time(group["daily_window_start"])
        window_end = _parse_time(group["daily_window_end"])
        min_gap_seconds = int(group["min_prompt_gap_minutes"]) * 60
        prompts_per_day = int(group["prompts_per_day"])

        members = (
            sb.table("group_members")
            .select("user_id, profiles(timezone)")
            .eq("group_id", group["id"])
            .execute()
            .data
            or []
        )

        for member in members:
            counts["members"] += 1
            user_id = member["user_id"]
            tz = (member.get("profiles") or {}).get("timezone") or "UTC"

            for local_date in _local_dates_in_range(now, horizon, tz):
                if not (group_start <= local_date <= group_end):
                    continue

                ranges = local_window_to_utc_ranges(local_date, window_start, window_end, tz)
                clipped = _clip_ranges(ranges, now, horizon)
                if not clipped:
                    continue

                existing_rows = (
                    sb.table("prompts")
                    .select("scheduled_at, status")
                    .eq("group_id", group["id"])
                    .eq("user_id", user_id)
                    .eq("local_date", local_date.isoformat())
                    .execute()
                    .data
                    or []
                )
                # Count ALL statuses including missed (see module docstring).
                counted = [
                    r for r in existing_rows if r["status"] in _STATUSES_COUNTING_TOWARD_DAILY
                ]
                needed = prompts_per_day - len(counted)
                if needed <= 0:
                    continue

                existing_utc = [_parse_dt(r["scheduled_at"]) for r in existing_rows]
                slots = _bucket_jitter_slots(clipped, needed, min_gap_seconds, existing_utc, rng)
                if not slots:
                    continue

                rows = [_build_prompt_row(group, user_id, local_date, s, rng) for s in slots]
                sb.table("prompts").insert(rows).execute()
                counts["prompts_inserted"] += len(rows)

    return counts


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    counts = run_tick(get_supabase())
    log.info("generator tick OK: %s", counts)


if __name__ == "__main__":
    main()
