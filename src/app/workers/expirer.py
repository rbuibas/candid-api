"""Prompt expirer — every 60s.

Selects prompts with status='active' joined to their group's window
settings, computes each row's late_deadline, and flips status='missed' for
any row past that deadline.

This is the one place where a `late` UI state in /prompts/active eventually
becomes a `missed` DB row. The confirm handler does NOT flip status='missed'
itself — it just refuses to insert a post past late_deadline and lets the
expirer catch up on its next tick.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from supabase import Client

from app.clients.supabase import get_supabase

log = logging.getLogger(__name__)


def run_tick(sb: Client, *, now: datetime | None = None) -> dict[str, int]:
    """Flip stale active prompts to missed."""
    now = now or datetime.now(UTC)

    rows = (
        sb.table("prompts")
        .select("id, dispatched_at, groups(response_window_seconds, late_window_seconds)")
        .eq("status", "active")
        .not_.is_("dispatched_at", "null")
        .execute()
        .data
        or []
    )

    expired_ids: list[str] = []
    for row in rows:
        dispatched_at = datetime.fromisoformat(row["dispatched_at"]).astimezone(UTC)
        group = row["groups"] or {}
        total = int(group.get("response_window_seconds", 0)) + int(
            group.get("late_window_seconds", 0)
        )
        if now > dispatched_at + timedelta(seconds=total):
            expired_ids.append(row["id"])

    if expired_ids:
        sb.table("prompts").update({"status": "missed"}).in_("id", expired_ids).execute()

    return {"prompts_missed": len(expired_ids)}


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    counts = run_tick(get_supabase())
    log.info("expirer tick OK: %s", counts)


if __name__ == "__main__":
    main()
