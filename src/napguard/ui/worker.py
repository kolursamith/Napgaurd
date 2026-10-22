"""Background work for the GUI without ever touching Tk from a worker thread.

Root cause of the Ubuntu GUI smoke failure (87b89a1): workers handed results back with
`root.after()` from their own thread. With Ubuntu's threaded Tcl, `_tkinter` serves such a
call only while the main thread runs `mainloop()`; otherwise it raises "main thread is not
in main loop", and that error was swallowed, so no result ever arrived (`--smoke` drives
the window with `update()`, not `mainloop()`).

Now worker threads only put results into a thread-safe queue. The main thread drains it
every POLL_MS with an `after()` callback it scheduled itself, so every Tk call happens on
the Tk thread, under `mainloop()` and under `update()` alike.
"""

from __future__ import annotations

import queue
import threading
import traceback
from collections.abc import Callable
from typing import Any, Protocol

from napguard.daemon.ipc import IpcError

POLL_MS = 50


class Scheduler(Protocol):  # the part of tk.Tk the worker needs
    def after(self, ms: int, func: Callable[..., Any], *args: Any) -> str: ...


Job = tuple[Callable[[], Any], Callable[[Any], None], Callable[[IpcError], None]]


class Worker:
    def __init__(self, root: Scheduler) -> None:
        """Create on the Tk (main) thread; that is where the pump is scheduled."""
        self.root = root
        self.jobs: queue.Queue[Job] = queue.Queue()
        self.results: queue.Queue[tuple[Callable[[Any], None], Any]] = queue.Queue()
        self.stopped = False
        threading.Thread(target=self._run, name="napguard-gui-worker", daemon=True).start()
        self.root.after(POLL_MS, self.pump)

    def submit(self, fn: Callable[[], Any], ok: Callable[[Any], None],
               err: Callable[[IpcError], None]) -> None:
        self.jobs.put((fn, ok, err))

    def _run(self) -> None:  # worker thread: never calls Tk
        while True:
            fn, ok, err = self.jobs.get()
            try:
                result = fn()
            except IpcError as exc:
                self.results.put((err, exc))
            except Exception as exc:
                self.results.put((err, IpcError("INTERNAL", repr(exc),
                                                {"traceback": traceback.format_exc()})))
            else:
                self.results.put((ok, result))

    def pump(self) -> int:
        """Main thread: deliver finished results, then reschedule. Returns how many."""
        delivered = 0
        while True:
            try:
                callback, value = self.results.get_nowait()
            except queue.Empty:
                break
            callback(value)
            delivered += 1
        if not self.stopped:
            self.root.after(POLL_MS, self.pump)
        return delivered
