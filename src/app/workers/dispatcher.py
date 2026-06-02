"""Prompt dispatcher — every 60s.

Selects prompts with status='scheduled' and scheduled_at ≤ now, sends FCM
to all the user's devices, then flips status='active' + dispatched_at=now.
On invalid-token responses (FCM UnregisteredError / SenderIdMismatchError),
deletes the device row so the next tick doesn't try again.

Per-tick group cache avoids N+1 reads on `groups` when multiple prompts in
the same batch belong to the same group.

Phase 6 — lock re-check. A prompt is scheduled while its group is active but
may not be dispatched until a later tick, after the group has locked
(today's UTC date > end_date). Such a prompt is CANCELLED: flipped straight
to a terminal status='missed' (no push, no dispatched_at), so the expirer,
the feed, and /prompts/active all stay consistent. The lifecycle rule is
delegated to groups.compute_lifecycle — "locked" means the same thing here
as everywhere else.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

from supabase import Client

from app.clients import firebase
from app.clients.supabase import get_supabase
from app.services.groups import compute_lifecycle

log = logging.getLogger(__name__)

PUSH_TITLE = "Time to capture"


def _fetch_group_settings(sb: Client, group_id: str) -> dict[str, Any]:
    result = (
        sb.table("groups")
        .select("response_window_seconds, late_window_seconds, start_date, end_date")
        .eq("id", group_id)
        .maybe_single()
        .execute()
    )
    if not result or not result.data:
        # The dispatcher loop deals with stale prompt rows defensively — if
        # the group was deleted out from under us, return zeros so the data
        # payload still ships and the prompt still flips active. No dates means
        # the lock re-check below is skipped (can't determine lifecycle).
        return {"response_window_seconds": 0, "late_window_seconds": 0}
    return result.data


def _group_is_locked(settings: dict[str, Any]) -> bool:
    """True when the group's settings row carries dates and is past end_date.

    Returns False when the dates are absent (deleted group / defensive zeros)
    so we fall back to the pre-Phase-6 behaviour of dispatching the prompt.
    """
    start_raw = settings.get("start_date")
    end_raw = settings.get("end_date")
    if not start_raw or not end_raw:
        return False
    lifecycle = compute_lifecycle(
        date.fromisoformat(str(start_raw)),
        date.fromisoformat(str(end_raw)),
    )
    return lifecycle == "locked"


def _build_push_data(prompt: dict, group_settings: dict, dispatched_at: datetime) -> dict[str, Any]:
    """Payload shape per the Phase 4 contract addendum."""
    data: dict[str, Any] = {
        "prompt_id": prompt["id"],
        "group_id": prompt["group_id"],
        "media_type": prompt["media_type"],
        "dispatched_at": dispatched_at.isoformat(),
        "response_window_seconds": group_settings["response_window_seconds"],
        "late_window_seconds": group_settings["late_window_seconds"],
    }
    if prompt["media_type"] == "video" and prompt.get("target_video_length_seconds") is not None:
        data["target_video_length_seconds"] = prompt["target_video_length_seconds"]
    return data


def run_tick(sb: Client, *, now: datetime | None = None) -> dict[str, int]:
    """Dispatch every scheduled prompt whose scheduled_at has arrived."""
    now = now or datetime.now(UTC)
    counts = {
        "prompts_dispatched": 0,
        "tokens_sent": 0,
        "devices_pruned": 0,
        "prompts_cancelled_locked": 0,
    }

    ready = (
        sb.table("prompts")
        .select("*")
        .eq("status", "scheduled")
        .lte("scheduled_at", now.isoformat())
        .is_("dispatched_at", "null")
        .execute()
        .data
        or []
    )

    group_cache: dict[str, dict[str, Any]] = {}

    for prompt in ready:
        gid = prompt["group_id"]
        if gid not in group_cache:
            group_cache[gid] = _fetch_group_settings(sb, gid)
        settings = group_cache[gid]

        # Lock re-check: the group may have locked between scheduling and now.
        # Cancel the prompt to a terminal state instead of pushing it.
        if _group_is_locked(settings):
            sb.table("prompts").update({"status": "missed"}).eq("id", prompt["id"]).execute()
            log.info(
                "dispatcher cancelled prompt %s: group %s locked after scheduling",
                prompt["id"],
                gid,
            )
            counts["prompts_cancelled_locked"] += 1
            continue

        devices = (
            sb.table("devices").select("fcm_token").eq("user_id", prompt["user_id"]).execute().data
            or []
        )
        tokens = [d["fcm_token"] for d in devices]

        data = _build_push_data(prompt, settings, now)
        if tokens:
            result = firebase.send_push(tokens, data, title=PUSH_TITLE, body="")
            counts["tokens_sent"] += result.success_count
            if result.invalid_tokens:
                sb.table("devices").delete().in_("fcm_token", result.invalid_tokens).execute()
                counts["devices_pruned"] += len(result.invalid_tokens)

        # Flip the prompt to active even if the user has no devices — they
        # can still open the app and see it. Otherwise we'd be stuck in a
        # loop trying to re-dispatch forever.
        sb.table("prompts").update({"status": "active", "dispatched_at": now.isoformat()}).eq(
            "id", prompt["id"]
        ).execute()
        counts["prompts_dispatched"] += 1

    return counts


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    counts = run_tick(get_supabase())
    log.info("dispatcher tick OK: %s", counts)


if __name__ == "__main__":
    main()
