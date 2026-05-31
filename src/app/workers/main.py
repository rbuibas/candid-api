"""Background worker entrypoint — a single Render service.

We deliberately run ONE Render Background Worker (not three Cron Jobs).
APScheduler boots three jobs in this one process:

  - generator: every 60 minutes, plus a single startup tick.
  - dispatcher: every 60 seconds.
  - expirer:    every 60 seconds.

Why BlockingScheduler over BackgroundScheduler? This process exists solely
to run jobs — there is no FastAPI app to keep alive in parallel. Blocking
keeps the main thread on the scheduler so Render sees a healthy long-lived
process.

Each tick is wrapped in `_safe_tick`: a bad tick logs the exception and
returns, so one transient failure doesn't kill the whole worker.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from supabase import Client

from app.clients.supabase import get_supabase
from app.config import get_settings
from app.workers import dispatcher, expirer, generator

log = logging.getLogger(__name__)


def _safe_tick(name: str, fn: Callable[[Client], dict[str, int]], sb: Client) -> None:
    try:
        counts = fn(sb)
        log.info("%s tick OK: %s", name, counts)
    except Exception:
        log.exception("%s tick FAILED", name)


def main() -> None:
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    sb = get_supabase()
    sched = BlockingScheduler(timezone="UTC")
    sched.add_job(
        lambda: _safe_tick("generator", generator.run_tick, sb),
        "interval",
        minutes=60,
        coalesce=True,
        max_instances=1,
        id="generator",
        next_run_time=datetime.now(UTC),  # also run once at startup
    )
    sched.add_job(
        lambda: _safe_tick("dispatcher", dispatcher.run_tick, sb),
        "interval",
        seconds=60,
        coalesce=True,
        max_instances=1,
        id="dispatcher",
    )
    sched.add_job(
        lambda: _safe_tick("expirer", expirer.run_tick, sb),
        "interval",
        seconds=60,
        coalesce=True,
        max_instances=1,
        id="expirer",
    )
    log.info("candid-workers booting: generator(60m), dispatcher(60s), expirer(60s)")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("candid-workers shutting down")
        sched.shutdown(wait=False)


if __name__ == "__main__":
    main()
