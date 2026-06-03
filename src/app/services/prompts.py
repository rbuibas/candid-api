"""Prompts service — timing helpers + read paths + dev fire-prompt.

Pure helpers (local_window_to_utc_ranges, compute_deadlines, compute_state)
are shared by the read endpoints, the workers, and the confirm extension.

Read paths (get_active_for_user, get_for_user) join `groups` to fold the
response/late windows into pre-computed deadlines and a UI state, so the
client renders countdowns without recomputing lateness.

fire_prompt_now is the impl for POST /dev/fire-prompt — creates a
status='active', dispatched_at=now prompt for the caller and pushes
immediately. The router gates the endpoint behind DEV_ENDPOINTS_ENABLED.
"""

import random
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from supabase import Client

from app.clients import firebase
from app.models.prompt import (
    FirePromptRequest,
    Prompt,
    PromptMediaType,
    PromptStatus,
    PromptUIState,
    PromptView,
    TriggerPromptRequest,
)


class PromptNotFoundError(Exception):
    """No prompt row with this id."""


class PromptNotAccessibleError(Exception):
    """Prompt exists but the caller doesn't own it."""


class PromptNotDispatchedError(Exception):
    """Prompt exists and is owned by the caller but `dispatched_at` is still null.

    Returned for /prompts/{id} when status='scheduled': the client should not
    see a deadline yet. Router maps to 409.
    """


class GroupNotFoundError(Exception):
    """Caller is not a member of the requested group, or no such group.

    Used by fire_prompt_now. Routers map to 404 (anti-leak).
    """


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


# --- Read paths -------------------------------------------------------


def _parse_dt(raw: str) -> datetime:
    """Parse a Postgres timestamptz string into an aware UTC datetime.

    PostgREST returns values like "2026-05-30T12:00:00+00:00"; fromisoformat
    handles the offset correctly. astimezone(UTC) normalizes any other offset.
    """
    return datetime.fromisoformat(raw).astimezone(UTC)


def _build_view(row: dict, now: datetime) -> PromptView:
    """Project a joined prompts+groups row into the PromptView read shape."""
    group = row["groups"]
    dispatched = _parse_dt(row["dispatched_at"])
    rws = int(group["response_window_seconds"])
    lws = int(group["late_window_seconds"])
    db_status = row.get("status", "active")
    on_time, late = compute_deadlines(dispatched, rws, lws)
    return PromptView(
        id=UUID(row["id"]),
        group_id=UUID(row["group_id"]),
        media_type=PromptMediaType(row["media_type"]),
        target_video_length_seconds=row.get("target_video_length_seconds"),
        dispatched_at=dispatched,
        on_time_deadline=on_time,
        late_deadline=late,
        state=PromptUIState.RESPONDED
        if db_status in (PromptStatus.RESPONDED, PromptStatus.LATE)
        else compute_state(now, dispatched, rws, lws),
    )


def get_active_for_user(sb: Client, user_id: UUID) -> list[PromptView]:
    """All actionable prompts for the caller across groups.

    `actionable` here means the DB row is status='active' (dispatcher has
    fired the push) and the computed UI state is not yet `missed`. A prompt
    whose late_deadline has passed but the expirer hasn't run yet is filtered
    out — the client never sees something it can't act on.
    """
    result = (
        sb.table("prompts")
        .select("*, groups(response_window_seconds, late_window_seconds)")
        .eq("user_id", str(user_id))
        .eq("status", "active")
        .not_.is_("dispatched_at", "null")
        .execute()
    )
    now = datetime.now(UTC)
    out: list[PromptView] = []
    for row in result.data or []:
        view = _build_view(row, now)
        if view.state is PromptUIState.MISSED:
            continue
        out.append(view)
    return out


def get_for_user(sb: Client, user_id: UUID, prompt_id: UUID) -> PromptView:
    """Single prompt lookup. Manual user-scoping — service-role bypasses RLS.

    Distinguishes:
      - not found → PromptNotFoundError (404)
      - exists but not caller's → PromptNotAccessibleError (403)
      - exists, caller's, still scheduled → PromptNotDispatchedError (409)
      - otherwise → PromptView with a computed UI `state` (which may be MISSED
        for a row the expirer hasn't yet caught up to, or a sensible state
        for a DB row already in 'responded'/'late'/'missed')
    """
    result = (
        sb.table("prompts")
        .select("*, groups(response_window_seconds, late_window_seconds)")
        .eq("id", str(prompt_id))
        .maybe_single()
        .execute()
    )
    if not result or not result.data:
        raise PromptNotFoundError()
    row = result.data
    if row["user_id"] != str(user_id):
        raise PromptNotAccessibleError()
    if row.get("dispatched_at") is None:
        raise PromptNotDispatchedError()
    return _build_view(row, datetime.now(UTC))


# --- Dev-only fire-prompt ---------------------------------------------


_FIRE_PUSH_TITLE = "Time to capture"


def _is_member(sb: Client, user_id: UUID, group_id: UUID) -> bool:
    result = (
        sb.table("group_members")
        .select("id")
        .eq("group_id", str(group_id))
        .eq("user_id", str(user_id))
        .maybe_single()
        .execute()
    )
    return bool(result and result.data)


def fire_prompt_now(
    sb: Client,
    user_id: UUID,
    payload: FirePromptRequest,
    *,
    rng: random.Random | None = None,
) -> Prompt:
    """Create an immediately-dispatched prompt for the caller and push it.

    Mirrors what the generator+dispatcher would produce, condensed into one
    request for hand-testing without waiting on cron.
    """
    rng = rng or random.Random()

    if not _is_member(sb, user_id, payload.group_id):
        raise GroupNotFoundError()

    group = (
        sb.table("groups")
        .select("max_video_length_seconds, response_window_seconds, late_window_seconds")
        .eq("id", str(payload.group_id))
        .maybe_single()
        .execute()
    )
    if not group or not group.data:
        raise GroupNotFoundError()
    g = group.data

    profile = (
        sb.table("profiles").select("timezone").eq("id", str(user_id)).maybe_single().execute()
    )
    tz = (profile.data or {}).get("timezone", "UTC") if profile else "UTC"
    now = datetime.now(UTC)
    local_today = now.astimezone(ZoneInfo(tz)).date()

    media_type = payload.media_type or rng.choice([PromptMediaType.PHOTO, PromptMediaType.VIDEO])
    target_video: int | None = None
    if media_type is PromptMediaType.VIDEO:
        max_video = int(g["max_video_length_seconds"])
        target_video = rng.randint(3, max_video) if max_video >= 3 else max_video

    row = {
        "id": str(uuid4()),
        "group_id": str(payload.group_id),
        "user_id": str(user_id),
        "scheduled_at": now.isoformat(),
        "dispatched_at": now.isoformat(),
        "local_date": local_today.isoformat(),
        "media_type": media_type.value,
        "target_video_length_seconds": target_video,
        "status": "active",
    }
    inserted = sb.table("prompts").insert(row).execute()
    if not inserted.data:
        raise RuntimeError("prompts insert returned no row")
    saved = inserted.data[0]

    devices = (
        sb.table("devices").select("fcm_token").eq("user_id", str(user_id)).execute().data or []
    )
    tokens = [d["fcm_token"] for d in devices]
    if tokens:
        data: dict = {
            "prompt_id": saved["id"],
            "group_id": saved["group_id"],
            "media_type": saved["media_type"],
            "dispatched_at": saved["dispatched_at"],
            "response_window_seconds": g["response_window_seconds"],
            "late_window_seconds": g["late_window_seconds"],
        }
        if target_video is not None:
            data["target_video_length_seconds"] = target_video
        result = firebase.send_push(tokens, data, title=_FIRE_PUSH_TITLE, body="")
        if result.invalid_tokens:
            sb.table("devices").delete().in_("fcm_token", result.invalid_tokens).execute()

    return Prompt.model_validate(saved)


def trigger_prompt_for_user(
    sb: Client,
    user_id: UUID,
    payload: TriggerPromptRequest,
    *,
    rng: random.Random | None = None,
) -> PromptView:
    """Create + immediately dispatch a prompt for the caller, returning PromptView.

    The impl behind POST /dev/prompts/trigger. Like fire_prompt_now it writes a
    status='active' row with dispatched_at=now so the deadlines anchor on server
    time, but it differs in two ways: it sends NO FCM push (the app navigates to
    capture straight off this HTTP response — it's a direct trigger, not a real
    dispatch), and it returns the PromptView read shape (computed deadlines + UI
    state) so the caller gets the same contract as /prompts/{id}.

    There is no `dispatched` prompt status in the DB enum; 'active' is the
    dispatched-and-actionable state. on_time/late deadlines are computed from the
    group windows, not stored.
    """
    rng = rng or random.Random()

    if not _is_member(sb, user_id, payload.group_id):
        raise GroupNotFoundError()

    group = (
        sb.table("groups")
        .select("max_video_length_seconds, response_window_seconds, late_window_seconds")
        .eq("id", str(payload.group_id))
        .maybe_single()
        .execute()
    )
    if not group or not group.data:
        raise GroupNotFoundError()
    g = group.data
    rws = int(g["response_window_seconds"])
    lws = int(g["late_window_seconds"])

    profile = (
        sb.table("profiles").select("timezone").eq("id", str(user_id)).maybe_single().execute()
    )
    tz = (profile.data or {}).get("timezone", "UTC") if profile else "UTC"
    now = datetime.now(UTC)
    local_today = now.astimezone(ZoneInfo(tz)).date()

    media_type = payload.media_type
    target_video: int | None = None
    if media_type is PromptMediaType.VIDEO:
        max_video = int(g["max_video_length_seconds"])
        target_video = rng.randint(3, max_video) if max_video >= 3 else max_video

    row = {
        "id": str(uuid4()),
        "group_id": str(payload.group_id),
        "user_id": str(user_id),
        "scheduled_at": now.isoformat(),
        "dispatched_at": now.isoformat(),
        "local_date": local_today.isoformat(),
        "media_type": media_type.value,
        "target_video_length_seconds": target_video,
        "status": "active",
    }
    inserted = sb.table("prompts").insert(row).execute()
    if not inserted.data:
        raise RuntimeError("prompts insert returned no row")
    saved = inserted.data[0]

    dispatched = _parse_dt(saved["dispatched_at"])
    on_time, late = compute_deadlines(dispatched, rws, lws)
    return PromptView(
        id=UUID(saved["id"]),
        group_id=UUID(saved["group_id"]),
        media_type=PromptMediaType(saved["media_type"]),
        target_video_length_seconds=saved.get("target_video_length_seconds"),
        dispatched_at=dispatched,
        on_time_deadline=on_time,
        late_deadline=late,
        state=compute_state(now, dispatched, rws, lws),
    )
