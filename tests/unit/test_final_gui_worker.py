"""GUI worker (Ubuntu 87b89a1: "9 screens rendered; daemon NOT reachable").

Root cause: worker threads called Tk (`root.after`) directly; outside `mainloop()` Ubuntu's
threaded Tcl refuses that ("main thread is not in main loop") and the result was dropped.
The worker must never call Tk from its thread; the main thread pumps results.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from napguard.daemon.ipc import IpcError
from napguard.ui.worker import POLL_MS, Worker


class FakeRoot:
    """Records which thread calls after(); runs callbacks only when pumped by the test."""

    def __init__(self) -> None:
        self.main = threading.get_ident()
        self.callers: list[int] = []
        self.pending: list[Callable[..., Any]] = []

    def after(self, ms: int, func: Callable[..., Any], *args: Any) -> str:
        self.callers.append(threading.get_ident())
        self.pending.append(lambda: func(*args))
        return "after#1"

    def run_pending(self) -> None:
        todo, self.pending = self.pending, []
        for fn in todo:
            fn()


def test_results_are_delivered_on_the_main_thread_only() -> None:
    root = FakeRoot()
    worker = Worker(root)
    got: list[tuple[str, Any, int]] = []
    worker.submit(lambda: 42, lambda r: got.append(("ok", r, threading.get_ident())),
                  lambda e: got.append(("err", e.code, threading.get_ident())))

    def fail() -> None:
        raise IpcError("DAEMON_UNAVAILABLE", "down")

    worker.submit(fail, lambda r: got.append(("ok", r, 0)),
                  lambda e: got.append(("err", e.code, threading.get_ident())))

    def boom() -> None:
        raise ValueError("bug")

    worker.submit(boom, lambda r: None, lambda e: got.append(("err", e.code,
                                                              threading.get_ident())))
    deadline = time.monotonic() + 5
    while len(got) < 3 and time.monotonic() < deadline:
        root.run_pending()  # what Tk's update()/mainloop() does with after() callbacks
        time.sleep(0.01)
    assert [g[:2] for g in got] == [("ok", 42), ("err", "DAEMON_UNAVAILABLE"),
                                    ("err", "INTERNAL")]
    assert {g[2] for g in got} == {root.main}  # callbacks ran on the main thread
    assert set(root.callers) == {root.main}  # the worker thread never called Tk
    assert POLL_MS <= 100


def test_pump_reschedules_itself_until_stopped() -> None:
    root = FakeRoot()
    worker = Worker(root)
    assert len(root.pending) == 1
    root.run_pending()
    assert len(root.pending) == 1  # rescheduled
    worker.stopped = True
    root.run_pending()
    assert root.pending == []
