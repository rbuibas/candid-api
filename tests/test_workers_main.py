"""Unit tests for workers/main._safe_tick.

The main() entrypoint itself starts BlockingScheduler, which is not
unit-testable. We cover the smaller-but-critical pieces: log + swallow
exceptions, log counts on success.
"""

import logging
from typing import Any

from app.workers.main import _safe_tick


def _fail(_sb: Any) -> dict[str, int]:
    raise RuntimeError("boom")


def _succeed(_sb: Any) -> dict[str, int]:
    return {"x": 1}


def test_safe_tick_swallows_exceptions(caplog: Any) -> None:
    caplog.set_level(logging.ERROR, logger="app.workers.main")
    _safe_tick("test", _fail, object())
    assert any("test tick FAILED" in r.message for r in caplog.records)


def test_safe_tick_logs_counts_on_success(caplog: Any) -> None:
    caplog.set_level(logging.INFO, logger="app.workers.main")
    _safe_tick("test", _succeed, object())
    assert any("test tick OK" in r.message and "x" in r.message for r in caplog.records)
