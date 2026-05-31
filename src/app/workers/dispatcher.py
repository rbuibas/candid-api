"""Prompt dispatcher — every 60s.

Selects prompts with status='scheduled' and scheduled_at ≤ now, sends FCM
to all the user's devices, then flips status='active' + dispatched_at=now.
On invalid-token responses (FCM UnregisteredError / SenderIdMismatchError),
deletes the device row so the next tick doesn't try again.

Per-tick group cache avoids N+1 reads on `groups` when multiple prompts in
the same batch belong to the same group.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from supabase import Client

from app.clients import firebase
from app.clients.supabase import get_supabase

log = logging.getLogger(__name__)

PUSH_TITLE = "Time to capture"


def _fetch_group_settings(sb: Client, group_id: str) -> dict[str, Any]:
    result = (
        sb.table("groups")
        .select("response_window_seconds, late_window_seconds")
        .eq("id", group_id)
        .maybe_single()
        .execute()
    )
    if not result or not result.data:
        # The dispatcher loop deals with stale prompt rows defensively — if
        # the group was deleted out from under us, return zeros so the data
        # payload still ships and the prompt still flips active.
        return {"response_window_seconds": 0, "late_window_seconds": 0}
    return result.data


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
    counts = {"prompts_dispatched": 0, "tokens_sent": 0, "devices_pruned": 0}

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
