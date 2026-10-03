"""Daily scheduling (plan section 8).

A tiny, dependency-free scheduler: compute the delay until the next occurrence
of a local hour, sleep, run. The run callable is injected so the loop is testable
and the CLI wires it to the real pipeline.

For production-grade scheduling prefer the OS: Windows Task Scheduler,
cron/systemd, or GitHub Actions. This is the "just works locally" option.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Callable

logger = logging.getLogger(__name__)


def next_run_delay(now: datetime, daily_hour: int) -> float:
    """Seconds from ``now`` until the next local ``daily_hour``:00.

    If that time is already past (or exactly now), schedules for tomorrow.
    """
    hour = max(0, min(23, int(daily_hour)))
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def serve(
    run: Callable[[], object],
    *,
    daily_hour: int = 9,
    run_immediately: bool = True,
    sleep: Callable[[float], object] = time.sleep,
    now: Callable[[], datetime] = datetime.now,
    max_iterations: int | None = None,
) -> None:
    """Run ``run`` now (optionally) and then once per day at ``daily_hour``."""
    if run_immediately:
        run()

    iterations = 0
    while True:
        delay = next_run_delay(now(), daily_hour)
        logger.info("Next scheduled run in %.0f seconds", delay)
        sleep(delay)
        run()
        iterations += 1
        if max_iterations is not None and iterations >= max_iterations:
            return
