"""Stage 06 (MVP): the user-level NapGuard daemon.

Runs as the ordinary user (never root), serves only this user over a 0600 Unix socket,
and owns the catalog, the manager, one operation executor, the scheduler, and the battery
monitor. DMTCP coordinators start only if the DAEMON itself was started with
`--i-accept-single-user-coordinator` (ADR 0006 conditions, extended to the MVP daemon by
ADR 0008, proposed); otherwise every launch, checkpoint and restore is refused, and
status, history and verification still work.
"""

from __future__ import annotations

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
    "workloads.list": {},
    "workloads.launch_demo": {"workload_class": (str, True)},
    "workloads.stop": {"workload_id": (str, True)},
    "checkpoint.request": {"workload_id": (str, True)},
    "checkpoints.list": {"workload_id": (str, False)},
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
    return params


class Service:
    def __init__(self, data_dir: Path, sock_path: Path, backend: ManagedBackend, *,
                 coordinator_acknowledged: bool, build: Callable[[Path], Path] = build_reference,
                 require_persistent: bool = True, battery: BatteryMonitor | None = None,
                 peer: Callable[..., int] | None = None, scheduler_tick_s: float = 5.0,
                 battery_poll_s: float = 30.0, manager_options: dict[str, Any] | None = None
                 ) -> None:
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        data_dir.chmod(0o700)
        fs = filesystem_type(data_dir)
        if require_persistent and fs not in PERSISTENT_FS:
            raise IpcError("STORAGE_NOT_PERSISTENT", f"{data_dir} is on {fs} (PLT-007)")
        self.coordinator_acknowledged = coordinator_acknowledged
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
            return self.manager.workloads()
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
            return self.manager.checkpoints(p.get("workload_id"))
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
        return problems

    def run_until_signal(self) -> int:
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        self.start()
        print(f"napguard daemon running (pid {os.getpid()}); socket {self.server.sock_path}",
              flush=True)
        while not stop.is_set():
            stop.wait(1.0)
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
