"""One worker thread runs every state-changing operation, so none overlap (FR-014, FR-015).

Requests return an operation ID at once (FR-010); progress and the outcome are recorded
in the catalog. A second checkpoint request for a workload that already has one queued
or running is merged into it.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

from napguard.checkpoint.base import BackendError
from napguard.storage.catalog import Catalog

from .manager import Manager, ManagerError

KINDS = ("launch", "stop", "checkpoint", "verify", "restore")


class Executor:
    def __init__(self, catalog: Catalog, manager: Manager) -> None:
        self.catalog = catalog
        self.manager = manager
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="napguard-ops", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def submit(self, kind: str, *, workload_id: str | None = None,
               checkpoint_id: str | None = None, trigger: str = "manual",
               params: dict[str, Any] | None = None) -> str:
        if kind not in KINDS:
            raise ValueError(kind)
        with self._lock:
            if kind == "checkpoint":
                pending = self.catalog.pending_operation(kind, workload_id)
                if pending is not None:
                    return pending  # merged (FR-014)
            op_id = self.catalog.add_operation(kind, workload_id, checkpoint_id, trigger, params)
            self._queue.put(op_id)
        return op_id

    def _dispatch(self, op: dict[str, Any]) -> dict[str, Any]:
        kind, wid, cid = op["kind"], op["workload_id"], op["checkpoint_id"]
        if kind == "launch":
            return self.manager.launch_demo(str(op["params"].get("workload_class", "")))
        if kind == "stop":
            return self.manager.stop(str(wid))
        if kind == "checkpoint":
            return self.manager.checkpoint(str(wid), str(op["trigger"] or "manual"))
        if kind == "verify":
            return self.manager.verify(str(cid))
        return self.manager.restore(str(cid))

    def run_one(self, op_id: str) -> None:
        op = self.catalog.operation(op_id)
        if op is None:
            return
        self.catalog.start_operation(op_id)
        started = time.monotonic()
        try:
            result = self._dispatch(op)
        except (BackendError, ManagerError) as exc:
            code = str(exc.code)
            detail = getattr(exc, "detail", {})
            self.catalog.finish_operation(op_id, False, int((time.monotonic() - started) * 1000),
                                          error_code=code, message=exc.message,
                                          result={"detail": detail})
            self.catalog.event("operation.failed", f"{op['kind']} failed: {code}: {exc.message}",
                               operation_id=op_id, code=code)
        except Exception as exc:  # an adapter bug: recorded, never reported as success
            self.catalog.finish_operation(op_id, False, int((time.monotonic() - started) * 1000),
                                          error_code="INTERNAL", message=repr(exc))
            self.catalog.event("operation.failed", f"{op['kind']} failed: INTERNAL: {exc!r}",
                               operation_id=op_id, code="INTERNAL")
        else:
            self.catalog.finish_operation(op_id, True, int((time.monotonic() - started) * 1000),
                                          message="ok", result=result)
        finally:
            self.manager.poll()

    def _run(self) -> None:
        while True:
            op_id = self._queue.get()
            if op_id is None:
                return
            self.run_one(op_id)

    def stop(self, timeout: float = 180.0) -> None:
        self._queue.put(None)
        if self._thread.is_alive():
            self._thread.join(timeout)
