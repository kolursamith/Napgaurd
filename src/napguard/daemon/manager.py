"""Stages 05 and 09 (MVP): supervise demo workloads, checkpoint them, verify, and restore.

Every operation goes through the Stage 03a backend (DMTCP 4.2.0 adapter) unchanged:
the fail-closed coordinator, the verified completion wait, artifact and manifest
validation, and the continuity rules of the verified self-test. Nothing here fakes a
process, a checkpoint, or a restore:

* only NapGuard's verified reference workloads (`c-single`, `c-threads`) can be launched;
  external processes are never attached (EXC-008);
* a restore is reported successful only when the restored process belongs to this
  restore, stays alive through the verification window (FR-026), and shows the ORIGINAL
  nonce with a counter rolled back into the checkpoint window and advancing;
* DMTCP writes each checkpoint over the previous image in the same directory, so only
  the newest generation of an instance is restorable; older rows become SUPERSEDED;
* a restored instance is not checkpointed again in the MVP (where DMTCP writes its next
  image is unverified); see ADR 0008.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from napguard.checkpoint import artifacts
from napguard.checkpoint.artifacts import PERSISTENT_FS
from napguard.checkpoint.base import (
    BackendError,
    BackendSession,
    CheckpointOptions,
    ErrorCode,
    RestoreOptions,
    SessionState,
    WorkloadSpec,
)
from napguard.checkpoint.compatibility import Compatibility
from napguard.checkpoint.manifest import MANIFEST_NAME, read_manifest, write_manifest
from napguard.core.proc import ProcId, alive, boot_id, filtered_env
from napguard.selftest import model
from napguard.selftest.run import CLASSES, TICK_MS, SelftestBackend, build_reference
from napguard.selftest.verify import Marks, Status, ownership_problems, verify_restore
from napguard.storage.catalog import Catalog, new_id, utc_now


class ManagedBackend(SelftestBackend, Protocol):
    def close_all(self) -> list[str]: ...


class ManagerError(Exception):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail or {}


def _private(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def _by_role(obs: Sequence[model.Observation]) -> dict[str, list[model.Observation]]:
    grouped: dict[str, list[model.Observation]] = {}
    for o in obs:
        grouped.setdefault(o.role, []).append(o)
    return grouped


class Manager:
    def __init__(self, catalog: Catalog, backend: ManagedBackend, data_dir: Path, *,
                 build: Callable[[Path], Path] = build_reference,
                 sleep: Callable[[float], None] = time.sleep,
                 verify_window_s: float = 3.0, report_timeout_s: float = 30.0,
                 step_timeout_s: float = 120.0) -> None:
        self.catalog = catalog
        self.backend = backend
        self.data_dir = data_dir
        self.build = build
        self.sleep = sleep
        self.verify_window_s = verify_window_s
        self.report_timeout_s = report_timeout_s
        self.step_timeout_s = step_timeout_s
        self.live: dict[str, tuple[str, BackendSession]] = {}  # instance id -> (workload, session)
        self._lock = threading.RLock()

    # ------------------------------------------------------------- helpers

    def _deadline(self) -> float:
        return time.monotonic() + self.step_timeout_s

    def _workload(self, workload_id: str) -> dict[str, Any]:
        row = self.catalog.one("SELECT * FROM workloads WHERE id=?", (workload_id,))
        if row is None:
            raise ManagerError("NOT_FOUND", f"unknown workload {workload_id}")
        return row

    def _live_instance(self, workload_id: str) -> tuple[str, BackendSession] | None:
        with self._lock:
            snapshot = list(self.live.items())  # IPC threads read while the worker writes
        for iid, (wid, session) in snapshot:
            if wid == workload_id:
                return iid, session
        return None

    def _roles(self, workload: dict[str, Any]) -> tuple[str, ...]:
        return CLASSES[workload["workload_class"]].roles

    def _wait_reports(self, report: Path, roles: Sequence[str], start_line: int, count: int,
                      session: BackendSession, step: str) -> bool:
        end = time.monotonic() + self.report_timeout_s
        while True:
            grouped = _by_role(model.read_reports(report, start_line))
            if all(len(grouped.get(r, [])) >= count for r in roles):
                return True
            self.backend.audit(session, step)
            if time.monotonic() >= end:
                return False
            self.sleep(0.25)

    def _set_instance(self, iid: str, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.catalog.tx() as db:
            db.execute(f"UPDATE instances SET {cols} WHERE id=?", (*fields.values(), iid))

    # -------------------------------------------------------------- launch

    def launch_demo(self, workload_class: str) -> dict[str, Any]:
        """Stage 05: launch one verified reference workload under the backend."""
        if workload_class not in CLASSES:
            raise ManagerError("UNSUPPORTED_WORKLOAD",
                               f"only verified reference classes {sorted(CLASSES)} can be launched")
        cls = CLASSES[workload_class]
        wid = new_id("wl")
        work = _private(self.data_dir / "workloads" / wid)
        _private(work / "reports")
        report = work / "reports" / "managed.txt"
        binary = self.build(work)
        argv = [str(binary), "--report", str(report), "--label", wid, "--tick-ms", str(TICK_MS),
                "--report-every", str(cls.report_every), *cls.args]
        spec = WorkloadSpec(wid, binary, argv, work, filtered_env(), reference_class=workload_class)
        assessed = self.backend.assess(spec)
        if assessed.level is not Compatibility.VERIFIED:
            raise ManagerError("UNSUPPORTED_WORKLOAD",
                               f"assessment is {assessed.level}: the MVP runs only VERIFIED reference "
                               "workloads on the Stage 02-verified platform", assessed.to_json())
        iid = new_id("in")
        session_dir = work / "session-launch-1"
        with self.catalog.tx() as db:
            db.execute("INSERT INTO workloads VALUES (?,?,?,?,?,?,?)",
                       (wid, f"demo {workload_class}", workload_class, json.dumps(argv), str(work),
                        str(report), utc_now()))
            db.execute("INSERT INTO instances(id, workload_id, origin, session_dir, state, "
                       "started_utc) VALUES (?,?, 'LAUNCH', ?, 'STARTING', ?)",
                       (iid, wid, str(session_dir), utc_now()))
        try:
            session = self.backend.launch(spec, session_dir, self._deadline())
        except BackendError:
            self._set_instance(iid, state="FAILED", ended_utc=utc_now())
            raise
        with self._lock:
            self.live[iid] = (wid, session)
        if not self._wait_reports(report, cls.roles, 0, 3, session, "launch"):
            self._stop_session(iid, session, "FAILED")
            raise ManagerError("LAUNCH_FAILED", "the demo workload did not report within "
                               f"{self.report_timeout_s:.0f} s")
        nonce = model.read_reports(report)[0].nonce
        self._set_instance(iid, state="RUNNING", pid=session.pid,
                           start_ticks=session.pid_start_ticks, uid=os.getuid(), boot_id=boot_id())
        self.catalog.event("workload.launched", f"launched demo {workload_class} ({wid})",
                           workload_id=wid, instance_id=iid, pid=session.pid, nonce=nonce)
        return {"workload_id": wid, "instance_id": iid, "pid": session.pid, "nonce": nonce}

    # ---------------------------------------------------------------- stop

    def _stop_session(self, iid: str, session: BackendSession, state: str) -> bool:
        gone = False
        try:
            gone = self.backend.stop(session)
        finally:
            problems = self.backend.cleanup(session, None)
            with self._lock:
                self.live.pop(iid, None)
            self._set_instance(iid, state=state if gone else "FAILED", ended_utc=utc_now(),
                               exit_info=json.dumps({"stopped": gone, "cleanup": problems}))
        return gone

    def stop(self, workload_id: str) -> dict[str, Any]:
        """Stage 05: terminate the workload's live instance (process group) and its coordinator."""
        workload = self._workload(workload_id)
        found = self._live_instance(workload_id)
        if found is None:
            raise ManagerError("NOT_RUNNING", f"workload {workload_id} has no live instance")
        iid, session = found
        gone = self._stop_session(iid, session, "STOPPED")
        self.sleep(0.3)  # nothing after this can be output of the stopped instance
        kill_line = model.count_lines(Path(workload["report_path"]))
        self._set_instance(iid, kill_line=kill_line)
        self.catalog.event("workload.stopped", f"stopped {workload_id} (instance {iid})",
                           workload_id=workload_id, instance_id=iid, confirmed_dead=gone)
        if not gone:
            raise ManagerError("INTERNAL", "the instance could not be confirmed terminated")
        return {"workload_id": workload_id, "instance_id": iid, "stopped": gone}

    # ---------------------------------------------------------- checkpoint

    def checkpoint(self, workload_id: str, trigger: str = "manual") -> dict[str, Any]:
        """Stage 03a checkpoint + Stage 04 publication into the catalog."""
        workload = self._workload(workload_id)
        found = self._live_instance(workload_id)
        if found is None:
            raise ManagerError("NOT_RUNNING", f"workload {workload_id} has no live instance")
        iid, session = found
        inst = self.catalog.one("SELECT * FROM instances WHERE id=?", (iid,))
        if inst is None or inst["origin"] != "LAUNCH":
            raise ManagerError("INVALID_STATE", "a RESTORED instance is not checkpointed in the MVP "
                               "(where DMTCP writes its next image is unverified; ADR 0008)")
        report = Path(workload["report_path"])
        before = model.read_reports(report)
        lower = {r: max(o.counter for o in obs) for r, obs in _by_role(before).items()}
        ckpt_dir = Path(session.data["checkpoint_dir"])
        capture = self.backend.checkpoint(session, ckpt_dir, CheckpointOptions(), self._deadline())
        fs = str(capture.restore_hints.get("filesystem", "?"))
        if fs not in PERSISTENT_FS:
            raise ManagerError("CHECKPOINT_FAILED", f"checkpoint directory is on {fs}, not a "
                               "persistent filesystem (PLT-007)")
        with artifacts.private_dir(ckpt_dir) as dfd:  # the previous generation's manifest
            try:
                os.unlink(MANIFEST_NAME, dir_fd=dfd)
            except FileNotFoundError:
                pass
        path = write_manifest(ckpt_dir, self.backend.capture_record(capture))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        done_line = model.count_lines(report)
        cid = new_id("ck")
        marks = {"nonce": before[-1].nonce if before else None, "lower": lower,
                 "done_line": done_line}
        images = [a for a in capture.artifacts if a.role.value == "IMAGE"]
        with self.catalog.tx() as db:
            gen = db.execute("SELECT COUNT(*) FROM checkpoints WHERE instance_id=?",
                             (iid,)).fetchone()[0] + 1
            db.execute("UPDATE checkpoints SET state='SUPERSEDED' WHERE instance_id=? "
                       "AND state='PUBLISHED'", (iid,))
            db.execute("INSERT INTO checkpoints(id, workload_id, instance_id, generation, trigger, "
                       "state, directory, manifest_sha256, total_bytes, capture_ms, backend, "
                       "backend_version, created_utc, marks) "
                       "VALUES (?,?,?,?,?, 'PUBLISHED', ?,?,?,?,?,?,?,?)",
                       (cid, workload_id, iid, gen, trigger, str(ckpt_dir), digest,
                        sum(a.size for a in images), capture.capture_ms,
                        "dmtcp", self.backend.capture_record(capture).backend_version,
                        utc_now(), json.dumps(marks)))
        self.catalog.event("checkpoint.published",
                           f"checkpoint {cid} of {workload_id} (generation {gen}, {trigger})",
                           workload_id=workload_id, checkpoint_id=cid,
                           bytes=sum(a.size for a in images), capture_ms=capture.capture_ms)
        return {"checkpoint_id": cid, "generation": gen, "capture_ms": capture.capture_ms,
                "bytes": sum(a.size for a in images)}

    # ------------------------------------------------------------- recovery

    def verify(self, checkpoint_id: str) -> dict[str, Any]:
        """Stage 09: manifest digest from the catalog, strict manifest, re-hashed artifacts."""
        row = self.catalog.one("SELECT * FROM checkpoints WHERE id=?", (checkpoint_id,))
        if row is None:
            raise ManagerError("NOT_FOUND", f"unknown checkpoint {checkpoint_id}")
        if row["state"] == "SUPERSEDED":
            raise ManagerError("INVALID_STATE", "superseded: DMTCP overwrote this image with a newer "
                               "generation, so it can no longer be restored")
        directory = Path(row["directory"])
        try:
            data = (directory / MANIFEST_NAME).read_bytes()
        except OSError as exc:
            self.catalog.set_checkpoint_state(checkpoint_id, "MISSING")
            raise ManagerError("CORRUPT", f"manifest missing: {exc.strerror}") from exc
        try:
            if hashlib.sha256(data).hexdigest() != row["manifest_sha256"]:
                raise BackendError(ErrorCode.CORRUPT, "manifest does not match the catalog digest")
            record = read_manifest(directory)
            artifacts.verify(directory, record.artifacts)
        except BackendError as exc:
            self.catalog.set_checkpoint_state(checkpoint_id, "CORRUPT")
            self.catalog.event("checkpoint.corrupt", f"{checkpoint_id}: {exc.message}",
                               checkpoint_id=checkpoint_id, code=str(exc.code))
            raise
        with self.catalog.tx() as db:
            db.execute("UPDATE checkpoints SET verified_utc=? WHERE id=?", (utc_now(), checkpoint_id))
        self.catalog.event("checkpoint.verified", f"{checkpoint_id} integrity verified",
                           checkpoint_id=checkpoint_id)
        return {"checkpoint_id": checkpoint_id, "artifacts": len(record.artifacts), "ok": True}

    def restore(self, checkpoint_id: str) -> dict[str, Any]:
        """Stage 09: genuine DMTCP restore, then FR-026 and continuity verification."""
        row = self.catalog.one("SELECT * FROM checkpoints WHERE id=?", (checkpoint_id,))
        if row is None:
            raise ManagerError("NOT_FOUND", f"unknown checkpoint {checkpoint_id}")
        if row["state"] != "PUBLISHED":
            raise ManagerError("INVALID_STATE", f"checkpoint is {row['state']}; only the newest "
                               "PUBLISHED generation can be restored")
        workload = self._workload(row["workload_id"])
        if self._live_instance(workload["id"]) is not None:
            raise ManagerError("CONFLICT", "the workload has a live instance: stop it first (FR-025)")
        source = self.catalog.one("SELECT * FROM instances WHERE id=?", (row["instance_id"],))
        if source is None or source["state"] != "STOPPED" or source["kill_line"] is None:
            raise ManagerError("INVALID_STATE", "the checkpointed instance was not confirmed "
                               "stopped; it cannot be told apart from a restore")
        self.verify(checkpoint_id)
        directory = Path(row["directory"])
        record = read_manifest(directory)
        report = Path(workload["report_path"])
        roles = self._roles(workload)
        count = len(self.catalog.query("SELECT id FROM instances WHERE workload_id=?",
                                       (workload["id"],))) + 1
        session_dir = Path(workload["work_dir"]) / f"session-restore-{count}"
        iid = new_id("in")
        start_line = model.count_lines(report)
        with self.catalog.tx() as db:
            db.execute("INSERT INTO instances(id, workload_id, origin, checkpoint_id, session_dir, "
                       "state, started_utc) VALUES (?,?, 'RESTORE', ?,?, 'STARTING', ?)",
                       (iid, workload["id"], checkpoint_id, str(session_dir), utc_now()))
        try:
            session = self.backend.restore(record, directory,
                                           RestoreOptions(session_dir=session_dir,
                                                          working_dir=Path(workload["work_dir"])),
                                           self._deadline())
        except BackendError:
            self._set_instance(iid, state="FAILED", ended_utc=utc_now())
            raise
        with self._lock:
            self.live[iid] = (workload["id"], session)

        def fail(code: str, why: str, **detail: Any) -> ManagerError:
            detail["diagnostics"] = self.backend.restore_diagnostics(session)
            self._stop_session(iid, session, "FAILED")
            self.catalog.event("restore.failed", f"restore of {checkpoint_id} failed: {why}",
                               checkpoint_id=checkpoint_id, code=code)
            return ManagerError(code, why, detail)

        if not self._wait_reports(report, roles, start_line, 3, session, "restore (waiting)"):
            raise fail("RESTORE_FAILED", "the restored workload did not report")
        original_dead = source["pid"] is not None and not alive(ProcId(
            int(source["pid"]), int(source["start_ticks"]), int(source["uid"])))
        problems = ownership_problems(
            model.read_reports(report, start_line),
            original_kpids={int(source["pid"])} if original_dead else set(),
            adopt=lambda k: self.backend.adopt_restored(session, k))
        if problems:
            raise fail("VERIFY_FAILED", "the reporting process is not this restore's process: "
                       + problems[0], ownership=problems[:20])
        end = time.monotonic() + self.verify_window_s
        while time.monotonic() < end:
            if self.backend.status(session).state is not SessionState.ALIVE:
                raise fail("VERIFY_FAILED", "the restored process exited during verification")
            self.backend.audit(session, "verification window")
            self.sleep(0.25)
        marks = json.loads(row["marks"])
        lines = model.read_reports(report)
        original = [o for o in lines if o.line_no <= int(source["kill_line"])
                    and o.nonce == marks["nonce"]]
        after_ckpt = [o for o in original if o.line_no > int(marks["done_line"])]
        restored = model.read_reports(report, start_line)
        checks = []
        for role in roles:
            mine = [o for o in after_ckpt if o.role == role]
            kill = [o.counter for o in original if o.role == role]
            if not mine or not kill:
                raise fail("VERIFY_FAILED", f"no post-checkpoint reports for role {role}")
            check = verify_restore(
                role=role, original=original,
                marks=Marks(int(marks["lower"][role]), min(o.counter for o in mine), max(kill)),
                restored=restored, original_dead=True,
                ballast_mib=CLASSES[workload["workload_class"]].ballast_mib)
            checks.append(check.to_json())
            if check.status is not Status.PASS:
                raise fail("VERIFY_FAILED", check.detail, checks=checks)
        self._set_instance(iid, state="RUNNING", pid=session.pid, start_ticks=session.pid_start_ticks,
                           uid=os.getuid(), boot_id=boot_id())
        with self.catalog.tx() as db:
            db.execute("UPDATE checkpoints SET restore_verified_utc=? WHERE id=?",
                       (utc_now(), checkpoint_id))
        self.catalog.event("restore.verified", f"restored {checkpoint_id}: continuity verified "
                           "(original nonce, rolled-back counter, progress)",
                           checkpoint_id=checkpoint_id, instance_id=iid, pid=session.pid)
        return {"checkpoint_id": checkpoint_id, "instance_id": iid, "pid": session.pid,
                "continuity": checks}

    # --------------------------------------------------------- monitoring

    def poll(self) -> None:
        """Stage 05: notice instances that exited on their own (FR-008)."""
        for iid, (wid, session) in list(self.live.items()):
            state = self.backend.status(session)
            if state.state is SessionState.ALIVE:
                continue
            problems = self.backend.cleanup(session, None)
            with self._lock:
                self.live.pop(iid, None)
            info = {"exit_code": state.exit_code, "signal": state.signal, "cleanup": problems}
            self._set_instance(iid, state="EXITED", ended_utc=utc_now(), exit_info=json.dumps(info))
            self.catalog.event("workload.exited", f"instance {iid} of {wid} exited",
                               workload_id=wid, instance_id=iid, exit_code=state.exit_code,
                               signal=state.signal, cleanup=problems)

    def eligible_for_auto(self) -> list[tuple[str, str]]:
        """(workload id, instance started_utc) of live LAUNCH instances (restored ones are not)."""
        found = []
        for iid, (wid, _) in list(self.live.items()):
            inst = self.catalog.one("SELECT origin, state, started_utc FROM instances WHERE id=?",
                                    (iid,))
            if inst and inst["origin"] == "LAUNCH" and inst["state"] == "RUNNING":
                found.append((wid, str(inst["started_utc"])))
        return found

    def workloads(self) -> list[dict[str, Any]]:
        rows = self.catalog.query("SELECT * FROM workloads ORDER BY created_utc DESC")
        for w in rows:
            w["argv"] = json.loads(w["argv"])
            inst = self.catalog.one("SELECT * FROM instances WHERE workload_id=? ORDER BY "
                                    "started_utc DESC, rowid DESC LIMIT 1", (w["id"],))
            w["instance"] = inst
            w["live"] = self._live_instance(w["id"]) is not None
        return rows

    def checkpoints(self, workload_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM checkpoints"
        args: tuple[Any, ...] = ()
        if workload_id:
            sql += " WHERE workload_id=?"
            args = (workload_id,)
        rows = self.catalog.query(sql + " ORDER BY created_utc DESC, rowid DESC", args)
        for r in rows:
            r["marks"] = json.loads(r["marks"])
        return rows

    def shutdown(self) -> list[str]:
        """Stop every managed instance and coordinator (MVP: the daemon owns them; ADR 0008)."""
        problems: list[str] = []
        for iid, (wid, session) in list(self.live.items()):
            try:
                self._stop_session(iid, session, "STOPPED")
            except BackendError as exc:
                problems.append(f"{wid}: {exc.message}")
        problems += self.backend.close_all()
        return problems
