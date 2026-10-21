"""Stage 07 (MVP): periodic automatic checkpoints for eligible managed workloads.

Disabled by default. The interval is at least 60 s (FR-013). Every checkpoint goes
through the single operation executor, so automatic and manual checkpoints never overlap,
and a due workload with a pending checkpoint is merged instead of queued twice.
"""

from __future__ import annotations

import calendar
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

MIN_INTERVAL_S = 60


def epoch(utc: str) -> float:
    return float(calendar.timegm(time.strptime(utc, "%Y-%m-%dT%H:%M:%SZ")))


def due(now: float, interval_s: int, eligible: Sequence[tuple[str, str]],
        last_checkpoint: dict[str, str]) -> list[str]:
    """Workloads whose newest checkpoint (or start) is at least `interval_s` old."""
    interval = max(MIN_INTERVAL_S, int(interval_s))
    found = []
    for workload_id, started_utc in eligible:
        since = last_checkpoint.get(workload_id, started_utc)
        if now - epoch(since) >= interval:
            found.append(workload_id)
    return found


class Scheduler:
    def __init__(self, settings: Callable[[], dict[str, Any]],
                 eligible: Callable[[], list[tuple[str, str]]],
                 last_checkpoint: Callable[[], dict[str, str]],
                 submit: Callable[[str], str], tick_s: float = 5.0,
                 clock: Callable[[], float] = time.time) -> None:
        self.settings, self.eligible, self.last_checkpoint = settings, eligible, last_checkpoint
        self.submit, self.tick_s, self.clock = submit, tick_s, clock
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="napguard-scheduler", daemon=True)

    def tick(self) -> list[str]:
        s = self.settings()
        if not s.get("auto_checkpoint_enabled"):
            return []
        workloads = due(self.clock(), int(s["auto_checkpoint_interval_s"]), self.eligible(),
                        self.last_checkpoint())
        return [self.submit(w) for w in workloads]

    def _run(self) -> None:
        while not self._stop.wait(self.tick_s):
            self.tick()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
