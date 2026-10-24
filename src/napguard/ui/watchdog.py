"""Bounded diagnostics for a stalled Tk event loop."""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

HEARTBEAT_MS = 500
STALL_SECONDS = 5.0
CHECK_SECONDS = 1.0
MAX_LOG_BYTES = 256 * 1024


class Scheduler(Protocol):
    def after(self, ms: int, func: Callable[..., Any], *args: Any) -> str: ...


def default_log_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "napguard" / "gui-hang.log"


class ResponsivenessWatchdog:
    """Observe a main-thread heartbeat; the monitor never calls Tk."""

    def __init__(self, root: Scheduler, path: Path | None = None, *,
                 clock: Callable[[], float] = time.monotonic,
                 stall_seconds: float = STALL_SECONDS,
                 start_thread: bool = True) -> None:
        self.root = root
        self.path = path or default_log_path()
        self.clock = clock
        self.stall_seconds = stall_seconds
        self._last_heartbeat = clock()
        self._captured = False
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self.root.after(HEARTBEAT_MS, self._heartbeat)
        if start_thread:
            threading.Thread(target=self._monitor, name="napguard-gui-watchdog",
                             daemon=True).start()

    def _heartbeat(self) -> None:
        if self._stopped.is_set():
            return
        with self._lock:
            self._last_heartbeat = self.clock()
            self._captured = False
        self.root.after(HEARTBEAT_MS, self._heartbeat)

    def _monitor(self) -> None:
        while not self._stopped.wait(CHECK_SECONDS):
            self.check()

    def check(self) -> bool:
        """Capture once per stall. Exposed for deterministic tests."""
        now = self.clock()
        with self._lock:
            if self._captured or now - self._last_heartbeat < self.stall_seconds:
                return False
            self._captured = True
            elapsed = now - self._last_heartbeat
        self._write_dump(now, elapsed)
        return True

    def _write_dump(self, now: float, elapsed: float) -> None:
        frames = sys._current_frames()
        names = {thread.ident: thread.name for thread in threading.enumerate()}
        lines = [f"\n=== Tk heartbeat stalled for {elapsed:.3f}s at monotonic {now:.3f} ===\n"]
        for ident, frame in frames.items():
            lines.append(f"--- thread {ident} ({names.get(ident, 'unknown')}) ---\n")
            lines.extend(traceback.format_stack(frame))
        payload = "".join(lines).encode("utf-8", errors="replace")[-MAX_LOG_BYTES:]
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            existing = self.path.read_bytes()
        except FileNotFoundError:
            existing = b""
        content = (existing + payload)[-MAX_LOG_BYTES:]
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, content)
            os.fsync(fd)
        finally:
            os.close(fd)

    def stop(self) -> None:
        self._stopped.set()
