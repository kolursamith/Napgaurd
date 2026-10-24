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
import sys
import threading
import traceback
from collections.abc import Callable
from typing import Any, Protocol

from napguard.daemon.ipc import IpcError

POLL_MS = 50
MAX_CALLBACKS_PER_PUMP = 32
MAX_PENDING_JOBS = 64


class Scheduler(Protocol):  # the part of tk.Tk the worker needs
    def after(self, ms: int, func: Callable[..., Any], *args: Any) -> str: ...


Job = tuple[Callable[[], Any], Callable[[Any], None], Callable[[IpcError], None]]


class Worker:
    def __init__(self, root: Scheduler,
                 callback_error: Callable[[IpcError], None] | None = None) -> None:
        """Create on the Tk (main) thread; that is where the pump is scheduled."""
        self.root = root
        self.callback_error = callback_error
        self.jobs: queue.Queue[Job | None] = queue.Queue(maxsize=MAX_PENDING_JOBS)
        self.results: queue.Queue[tuple[Callable[[Any], None], Any]] = queue.Queue()
        self.stopped = False
        threading.Thread(target=self._run, name="napguard-gui-worker", daemon=True).start()
        self.root.after(POLL_MS, self.pump)

    def submit(self, fn: Callable[[], Any], ok: Callable[[Any], None],
               err: Callable[[IpcError], None]) -> None:
        if self.stopped:
            return
        try:
            self.jobs.put_nowait((fn, ok, err))
        except queue.Full:
            self.results.put((err, IpcError(
                "BUSY", "The GUI work queue is full; wait for the current operation to finish.")))

    def _run(self) -> None:  # worker thread: never calls Tk
        while True:
            job = self.jobs.get()
            if job is None or self.stopped:
                return
            fn, ok, err = job
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
        try:
            while delivered < MAX_CALLBACKS_PER_PUMP:
                try:
                    callback, value = self.results.get_nowait()
                except queue.Empty:
                    break
                try:
                    callback(value)
                except Exception as exc:
                    failure = IpcError("INTERNAL", f"GUI callback failed: {exc!r}",
                                       {"traceback": traceback.format_exc()})
                    if self.callback_error is not None:
                        try:
                            self.callback_error(failure)
                        except Exception:
                            traceback.print_exc(file=sys.stderr)
                    else:
                        print(f"{failure.code}: {failure.message}", file=sys.stderr)
                        print(failure.details.get("traceback", ""), file=sys.stderr)
                delivered += 1
        finally:
            if not self.stopped:
                self.root.after(0 if not self.results.empty() else POLL_MS, self.pump)
        return delivered

    def stop(self) -> None:
        """Stop scheduling result delivery and ask the worker thread to exit."""
        if self.stopped:
            return
        self.stopped = True
        try:
            self.jobs.put_nowait(None)
        except queue.Full:
            # The daemon thread exits with the process if its bounded queue is still busy.
            pass
