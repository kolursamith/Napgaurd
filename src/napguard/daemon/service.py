"""Stage 06 (MVP): the user-level NapGuard daemon.

Runs as the ordinary user (never root), serves only this user over a 0600 Unix socket,
and owns the catalog, the manager, one operation executor, the scheduler, and the battery
monitor. DMTCP coordinators start only if the DAEMON itself was started with
`--i-accept-single-user-coordinator` (ADR 0006 conditions, extended to the MVP daemon by
ADR 0008, proposed); otherwise every launch, checkpoint and restore is refused, and
status, history and verification still work.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from napguard import __version__
from napguard.checkpoint.artifacts import PERSISTENT_FS, filesystem_type
from napguard.checkpoint.base import BackendError
from napguard.core import proc
from napguard.power.battery import BatteryMonitor
from napguard.selftest.run import CLASSES, build_reference
from napguard.storage.catalog import Catalog

from .ipc import IpcError, Server
from .manager import ManagedBackend, Manager, ManagerError
from .ops import Executor
from .scheduler import MIN_INTERVAL_S, Scheduler

Schema = dict[str, tuple[type, bool]]  # name -> (type, required)
SCHEMAS: dict[str, Schema] = {
    "daemon.status": {},
    "workloads.list": {"include_archived": (bool, False)},
    "workloads.launch_demo": {"workload_class": (str, True)},
    "workloads.stop": {"workload_id": (str, True)},
    "checkpoint.request": {"workload_id": (str, True)},
    "checkpoints.list": {"workload_id": (str, False), "include_archived": (bool, False)},
    "checkpoints.archive": {"checkpoint_id": (str, True), "archived": (bool, True)},
    "checkpoints.delete": {"checkpoint_id": (str, True), "confirm": (bool, True)},
    "history.archive": {"workload_id": (str, True), "archived": (bool, True)},
    "history.preview": {"workload_ids": (list, False)},
    "history.delete": {"workload_ids": (list, True), "confirm": (bool, True)},
    "history.reset_demo": {"confirm": (bool, True)},
    "operations.list": {"limit": (int, False), "kind": (str, False), "trigger": (str, False)},
    "scheduler.status": {},
    "scheduler.run_now": {},
    "diagnostics.export": {},
    "daemon.shutdown": {"confirm": (bool, True)},
    "checkpoints.verify": {"checkpoint_id": (str, True)},
    "recovery.restore": {"checkpoint_id": (str, True), "confirm": (bool, True)},
    "operations.get": {"operation_id": (str, True)},
    "events.list": {"limit": (int, False), "after_seq": (int, False)},
    "settings.get": {},
    "settings.set": {"auto_checkpoint_enabled": (bool, False),
                     "auto_checkpoint_interval_s": (int, False),
                     "battery_low_percent": (int, False),
                     "battery_critical_percent": (int, False)},
    "battery.status": {},
    "battery.simulate": {"percent": (int, False), "ac_online": (bool, False),
                         "clear": (bool, False)},
}


def validate(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Per-method schema: unknown method or field, wrong type, or a long string is refused."""
    schema = SCHEMAS.get(method)
    if schema is None:
        raise IpcError("UNKNOWN_METHOD", f"unknown method {method!r}")
    unknown = sorted(set(params) - set(schema))
    if unknown:
        raise IpcError("INVALID_ARGUMENT", f"unknown field(s) {unknown}")
    for name, (kind, required) in schema.items():
        if name not in params:
            if required:
                raise IpcError("INVALID_ARGUMENT", f"missing field {name!r}")
            continue
        value = params[name]
        if kind is int and (isinstance(value, bool) or not isinstance(value, int)):
            raise IpcError("INVALID_ARGUMENT", f"{name} must be an integer")
        if not isinstance(value, kind):
            raise IpcError("INVALID_ARGUMENT", f"{name} must be {kind.__name__}")
        if isinstance(value, str) and (len(value) > 256 or not value):
            raise IpcError("INVALID_ARGUMENT", f"{name} must be 1-256 characters")
        if isinstance(value, list) and (len(value) > 1000 or not all(
                isinstance(v, str) and 0 < len(v) <= 256 for v in value)):
            raise IpcError("INVALID_ARGUMENT", f"{name} must be a list of 1-256 character strings")
    return params


def _confirmed(p: dict[str, Any], what: str) -> None:
    if p.get("confirm") is not True:
        raise IpcError("INVALID_ARGUMENT", f"{what} needs confirm=true")


class Service:
    def __init__(self, data_dir: Path, sock_path: Path, backend: ManagedBackend, *,
                 coordinator_acknowledged: bool, build: Callable[[Path], Path] = build_reference,
                 require_persistent: bool = True, battery: BatteryMonitor | None = None,
                 peer: Callable[..., int] | None = None, scheduler_tick_s: float = 5.0,
                 battery_poll_s: float = 30.0, manager_options: dict[str, Any] | None = None
                 ) -> None:
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        data_dir.chmod(0o700)
        # One daemon per data directory; uninstall --purge checks this lock (not sockets).
        self._data_lock = os.open(data_dir / "daemon.lock",
                                  os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(self._data_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self._data_lock)
            raise IpcError("ALREADY_RUNNING", f"another NapGuard daemon uses {data_dir}") from exc
        fs = filesystem_type(data_dir)
        if require_persistent and fs not in PERSISTENT_FS:
            raise IpcError("STORAGE_NOT_PERSISTENT", f"{data_dir} is on {fs} (PLT-007)")
        self.coordinator_acknowledged = coordinator_acknowledged
        self.data_dir = data_dir
        self.sock_path = sock_path
        self.shutdown_requested = threading.Event()
        self.catalog = Catalog(data_dir / "catalog.db")
        leftovers = self._stop_leftovers()
        reconciled = self.catalog.reconcile()
        self.backend = backend
        self.capabilities = backend.probe()
        self.manager = Manager(self.catalog, backend, data_dir, build=build,
                               **(manager_options or {}))
        self.executor = Executor(self.catalog, self.manager)
        self.battery = battery or BatteryMonitor()
        self.scheduler = Scheduler(self.catalog.settings, self.manager.eligible_for_auto,
                                   self._last_checkpoints, self._submit_periodic,
                                   tick_s=scheduler_tick_s)
        self._battery_poll_s = battery_poll_s
        self._stop = threading.Event()
        server_kwargs: dict[str, Any] = {"audit": self._audit}
        if peer is not None:
            server_kwargs["peer"] = peer
        self.server = Server(sock_path, self.handle, **server_kwargs)
        self.catalog.event("daemon.started",
                           f"NapGuard daemon {__version__} started (coordinators "
                           f"{'ALLOWED in developer mode' if coordinator_acknowledged else 'REFUSED'})",
                           pid=os.getpid(), leftovers_stopped=leftovers, reconciled=reconciled)

    # ------------------------------------------------------------- startup

    def _stop_leftovers(self) -> list[str]:
        """Stop identity-matched processes of instances a previous daemon left RUNNING."""
        stopped: list[str] = []
        for row in self.catalog.query("SELECT session_dir FROM instances WHERE state IN "
                                      "('STARTING','RUNNING')"):
            state_file = Path(row["session_dir"]) / "session.json"
            try:
                done, _skipped = proc.cleanup_recorded(proc.SessionState.read(state_file))
            except (OSError, ValueError, KeyError):
                continue
            stopped += done
        return stopped

    def _audit(self, kind: str, data: dict[str, Any]) -> None:
        self.catalog.event(kind, f"IPC {kind}", **data)

    def _last_checkpoints(self) -> dict[str, str]:
        rows = self.catalog.query("SELECT workload_id, MAX(created_utc) AS t FROM checkpoints "
                                  "GROUP BY workload_id")
        return {r["workload_id"]: r["t"] for r in rows}

    def _submit_periodic(self, workload_id: str) -> str:
        return self.executor.submit("checkpoint", workload_id=workload_id, trigger="periodic")

    # ------------------------------------------------------------- battery

    def battery_tick(self) -> list[str]:
        s = self.catalog.settings()
        reading, fired = self.battery.evaluate(int(s["battery_low_percent"]),
                                               int(s["battery_critical_percent"]))
        simulated = reading.source == "SIMULATED"
        ops = []
        for trigger in fired:
            label = "SIMULATED " if simulated else ""
            self.catalog.event(f"battery.{trigger}", f"{label}battery {trigger}: "
                               f"{reading.percent}%", simulated=simulated, percent=reading.percent)
            for workload_id, _ in self.manager.eligible_for_auto():
                ops.append(self.executor.submit("checkpoint", workload_id=workload_id,
                                                trigger=f"{trigger}_battery"
                                                + ("_simulated" if simulated else "")))
        return ops

    def _battery_loop(self) -> None:
        while not self._stop.wait(self._battery_poll_s):
            self.battery_tick()

    # ------------------------------------------------------------ handlers

    def handle(self, method: str, params: dict[str, Any]) -> Any:
        p = validate(method, params)
        if method == "daemon.status":
            return self.status()
        if method == "workloads.list":
            return self.manager.workloads(bool(p.get("include_archived", False)))
        if method == "workloads.launch_demo":
            if p["workload_class"] not in CLASSES:
                raise IpcError("UNSUPPORTED_WORKLOAD",
                               f"only verified reference classes {sorted(CLASSES)}")
            return self._op("launch", params={"workload_class": p["workload_class"]})
        if method == "workloads.stop":
            return self._op("stop", workload_id=p["workload_id"])
        if method == "checkpoint.request":
            return self._op("checkpoint", workload_id=p["workload_id"])
        if method == "checkpoints.list":
            return self.manager.checkpoints(p.get("workload_id"),
                                            bool(p.get("include_archived", False)))
        if method == "checkpoints.archive":
            kind = "archive_checkpoint" if p["archived"] else "unarchive_checkpoint"
            return self._op(kind, checkpoint_id=p["checkpoint_id"])
        if method == "checkpoints.delete":
            _confirmed(p, "deleting a checkpoint")
            return self._op("delete_checkpoint", checkpoint_id=p["checkpoint_id"])
        if method == "history.archive":
            return self._op("archive" if p["archived"] else "unarchive",
                            workload_id=p["workload_id"])
        if method == "history.preview":
            ids = p.get("workload_ids")
            return self.executor.history.preview(None if ids is None else list(ids))
        if method == "history.delete":
            _confirmed(p, "deleting history")
            return self._op("delete_history", params={"workload_ids": list(p["workload_ids"])})
        if method == "history.reset_demo":
            _confirmed(p, "resetting the demo")
            self.battery.clear_simulation()
            return self._op("reset_demo")
        if method == "operations.list":
            return self._operations(p)
        if method == "scheduler.status":
            return self.scheduler_status()
        if method == "scheduler.run_now":
            return self._run_scheduler_now()
        if method == "diagnostics.export":
            return self.export_diagnostics()
        if method == "daemon.shutdown":
            _confirmed(p, "stopping the daemon")
            self.catalog.event("daemon.shutdown_requested", "shutdown requested over IPC; "
                               "managed workloads will be stopped")
            self.shutdown_requested.set()
            return {"stopping": True}
        if method == "checkpoints.verify":
            return self._op("verify", checkpoint_id=p["checkpoint_id"])
        if method == "recovery.restore":
            if p["confirm"] is not True:
                raise IpcError("INVALID_ARGUMENT", "restoring needs confirm=true (FR-023)")
            return self._op("restore", checkpoint_id=p["checkpoint_id"])
        if method == "operations.get":
            op = self.catalog.operation(p["operation_id"])
            if op is None:
                raise IpcError("NOT_FOUND", "unknown operation")
            return op
        if method == "events.list":
            return self.catalog.events(int(p.get("limit", 100)), int(p.get("after_seq", 0)))
        if method == "settings.get":
            return self.catalog.settings()
        if method == "settings.set":
            return self._set_settings(p)
        if method == "battery.status":
            return self.battery.current().to_json()
        return self._simulate(p)

    def _operations(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        clauses, args = [], []
        if "kind" in p:
            clauses.append("kind=?")
            args.append(p["kind"])
        if "trigger" in p:
            clauses.append("trigger LIKE ?")
            args.append(str(p["trigger"]) + "%")
        sql = "SELECT * FROM operations" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        rows = self.catalog.query(sql + " ORDER BY created_utc DESC, rowid DESC LIMIT ?",
                                  (*args, max(1, min(int(p.get("limit", 50)), 500))))
        for r in rows:
            r["params"] = json.loads(r["params"])
            r["result"] = json.loads(r["result"]) if r["result"] else None
        return rows

    def scheduler_status(self) -> dict[str, Any]:
        from .scheduler import epoch

        s = self.catalog.settings()
        interval = max(MIN_INTERVAL_S, int(s["auto_checkpoint_interval_s"]))
        last = self._last_checkpoints()
        upcoming = []
        for workload_id, started in self.manager.eligible_for_auto():
            base = epoch(last.get(workload_id, started))
            upcoming.append({"workload_id": workload_id, "last_checkpoint_utc": last.get(workload_id),
                             "next_due_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                           time.gmtime(max(base + interval,
                                                                           time.time())))
                             if s["auto_checkpoint_enabled"] else None})
        recent = self._operations({"kind": "checkpoint", "limit": 20})
        return {"enabled": bool(s["auto_checkpoint_enabled"]), "interval_s": interval,
                "tick_s": self.scheduler.tick_s, "eligible": upcoming,
                "recent_automatic": [r for r in recent if (r["trigger"] or "") != "manual"][:10],
                "note": "only running LAUNCH instances of verified reference workloads are "
                        "eligible; restored instances are not re-checkpointed (ADR 0008)"}

    def _run_scheduler_now(self) -> dict[str, Any]:
        eligible = self.manager.eligible_for_auto()
        if not eligible:
            raise IpcError("NOT_RUNNING", "no eligible running workload: launch a demo first")
        ops = [self.executor.submit("checkpoint", workload_id=w, trigger="scheduler_run_now")
               for w, _ in eligible]
        self.catalog.event("scheduler.run_now", f"scheduler pass requested now for "
                           f"{len(ops)} workload(s)", operations=ops)
        return {"operation_ids": ops, "state": "ACCEPTED"}

    def export_diagnostics(self) -> dict[str, Any]:
        out = self.data_dir / "diagnostics"
        out.mkdir(mode=0o700, exist_ok=True)
        path = out / time.strftime("napguard-diagnostics-%Y%m%dT%H%M%SZ.json", time.gmtime())
        bundle = {"status": self.status(), "scheduler": self.scheduler_status(),
                  "workloads": self.manager.workloads(include_archived=True),
                  "checkpoints": self.manager.checkpoints(include_archived=True),
                  "operations": self._operations({"limit": 200}),
                  "events": self.catalog.events(500)}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(bundle, fh, indent=2, sort_keys=True, default=str)
        self.catalog.event("diagnostics.exported", f"diagnostics written to {path}", path=str(path))
        return {"path": str(path), "bytes": path.stat().st_size}

    def _op(self, kind: str, **kwargs: Any) -> dict[str, Any]:
        return {"operation_id": self.executor.submit(kind, **kwargs), "state": "ACCEPTED"}

    def _set_settings(self, p: dict[str, Any]) -> dict[str, Any]:
        current = self.catalog.settings() | p
        if int(current["auto_checkpoint_interval_s"]) < MIN_INTERVAL_S:
            raise IpcError("INVALID_ARGUMENT", f"interval must be at least {MIN_INTERVAL_S} s")
        low, crit = int(current["battery_low_percent"]), int(current["battery_critical_percent"])
        if not 1 <= crit < low <= 99:
            raise IpcError("INVALID_ARGUMENT", "need 1 <= critical < low <= 99")
        result = self.catalog.set_settings(**p)
        self.catalog.event("settings.changed", f"settings changed: {sorted(p)}", **p)
        return result

    def _simulate(self, p: dict[str, Any]) -> dict[str, Any]:
        if p.get("clear"):
            self.battery.clear_simulation()
            self.catalog.event("battery.simulation_cleared", "battery simulation cleared",
                               simulated=True)
            return self.battery.current().to_json()
        if "percent" not in p:
            raise IpcError("INVALID_ARGUMENT", "percent is required unless clear=true")
        try:
            reading = self.battery.simulate(int(p["percent"]), bool(p.get("ac_online", False)))
        except ValueError as exc:
            raise IpcError("INVALID_ARGUMENT", str(exc)) from exc
        self.catalog.event("battery.simulated", f"SIMULATED battery reading {reading.percent}% "
                           f"(AC {'on' if reading.ac_online else 'off'})", simulated=True,
                           percent=reading.percent)
        self.battery_tick()
        return reading.to_json()

    def status(self) -> dict[str, Any]:
        caps = self.capabilities
        return {"version": __version__, "pid": os.getpid(), "uid": os.getuid(),
                "coordinator_mode": "developer (acknowledged at daemon start; ADR 0006/0008)"
                if self.coordinator_acknowledged else "refused (start the daemon with "
                "--i-accept-single-user-coordinator)",
                "backend": {"name": caps.name, "available": caps.available,
                            "version": caps.version, "verified": caps.verified,
                            "source_commit": caps.source_commit, "notes": caps.notes},
                "settings": self.catalog.settings(), "battery": self.battery.current().to_json(),
                "workloads": len(self.catalog.query("SELECT id FROM workloads")),
                "checkpoints": len(self.catalog.query("SELECT id FROM checkpoints WHERE "
                                                      "state='PUBLISHED'")),
                "data_dir": str(self.data_dir), "socket": str(self.sock_path),
                "supported_classes": sorted(CLASSES),
                "busy": self.catalog.one("SELECT kind, state FROM operations WHERE state IN "
                                         "('QUEUED','RUNNING') ORDER BY created_utc LIMIT 1"),
                "latest_restore": self._operations({"kind": "restore", "limit": 1})[:1],
                "latest_checkpoint": self.catalog.one(
                    "SELECT id, workload_id, generation, created_utc FROM checkpoints ORDER BY "
                    "created_utc DESC, rowid DESC LIMIT 1"),
                "live_instances": len(self.manager.live),
                "production_authorized": False}

    # ------------------------------------------------------------- running

    def start(self) -> None:
        self.server.bind()
        self.executor.start()
        self.scheduler.start()
        threading.Thread(target=self._battery_loop, name="napguard-battery", daemon=True).start()
        threading.Thread(target=self.server.serve_forever, name="napguard-ipc", daemon=True).start()

    def shutdown(self) -> list[str]:
        self._stop.set()
        self.scheduler.stop()
        self.server.close()
        self.executor.stop()
        problems = self.manager.shutdown()
        self.catalog.event("daemon.stopped", "daemon stopped; managed workloads were stopped",
                           problems=problems)
        self.catalog.close()
        os.close(self._data_lock)  # releases the data-directory lock
        return problems

    def run_until_signal(self) -> int:
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        self.start()
        print(f"napguard daemon running (pid {os.getpid()}); socket {self.server.sock_path}",
              flush=True)
        while not stop.is_set() and not self.shutdown_requested.is_set():
            stop.wait(0.5)
        problems = self.shutdown()
        print("napguard daemon stopped" + (f"; problems: {problems}" if problems else ""))
        return 1 if problems else 0


def wait_operation(call: Callable[..., Any], op_id: str, timeout: float = 300.0,
                   poll: float = 0.5) -> dict[str, Any]:
    end = time.monotonic() + timeout
    while True:
        op = call("operations.get", operation_id=op_id)
        if op["state"] in ("SUCCEEDED", "FAILED", "INTERRUPTED"):
            return dict(op)
        if time.monotonic() >= end:
            raise IpcError("TIMED_OUT", f"operation {op_id} still {op['state']}")
        time.sleep(poll)


ERRORS = (IpcError, ManagerError, BackendError)
