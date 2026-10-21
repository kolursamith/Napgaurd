"""TEST DOUBLE for the MVP tests: an in-memory counter workload behind the backend API.

It is NOT a checkpoint engine. Its "image" is a small JSON file holding the counters, so
the real artifact, manifest and catalog checks run against real files. It proves the
manager's, daemon's and scheduler's decision logic; only Ubuntu with DMTCP proves the
real checkpoint/restore path.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from napguard.checkpoint import artifacts
from napguard.checkpoint.base import (
    ArtifactRole,
    BackendCapabilities,
    BackendError,
    BackendSession,
    CaptureRecord,
    CaptureResult,
    CheckpointOptions,
    ErrorCode,
    RestoreOptions,
    SessionState,
    SessionStatus,
    WorkloadSpec,
)
from napguard.checkpoint.compatibility import CompatibilityReport, decide
from napguard.selftest import model


class Counter(threading.Thread):
    def __init__(self, report: Path, roles: tuple[str, ...], nonce: str, counters: dict[str, int],
                 kpid: int, die_after: float | None = None,
                 inflight_kpid: int | None = None) -> None:
        super().__init__(daemon=True)
        # Emulates a report composed BEFORE the checkpoint and written after the restore: the
        # first restored line of each role carries the (dead) original's kernel PID.
        self.inflight_kpid = inflight_kpid
        self.inflight_done: set[str] = set()
        self.report, self.roles, self.nonce, self.kpid = report, roles, nonce, kpid
        self.counters = dict(counters)
        self.chains = {r: model.DigestChain(nonce, r) for r in roles}
        self.halt = threading.Event()
        self.die_at = time.monotonic() + die_after if die_after else None
        self.lock = threading.Lock()

    def run(self) -> None:
        while not self.halt.is_set():
            if self.die_at is not None and time.monotonic() >= self.die_at:
                return
            out = []
            with self.lock:
                for role in self.roles:
                    self.counters[role] += 1
                    c = self.counters[role]
                    if c % 10 == 0:
                        kp = self.kpid
                        if self.inflight_kpid is not None and role not in self.inflight_done:
                            kp = self.inflight_kpid
                            self.inflight_done.add(role)
                        out.append(f"NGSTATE v1 run=t role={role} pid=1 kpid={kp} "
                                   f"nonce={self.nonce} counter={c} "
                                   f"digest={self.chains[role].at(c):016x} ballast=- "
                                   f"mono_ns={time.monotonic_ns()}\n")
            if out:
                with self.report.open("a", encoding="utf-8") as fh:
                    fh.writelines(out)
            time.sleep(0.001)

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return dict(self.counters)

    def stop(self) -> None:
        self.halt.set()
        self.join(5)


class FakeBackend:
    """Modes: genuine (default), fresh (restore starts a new process), dies, refuse-launch,
    inflight (first restored line per role carries the original's PID), stray-writer (a
    second, foreign process with the same nonce also writes after the restore)."""

    def __init__(self, mode: str = "genuine") -> None:
        self.mode = mode
        self.sessions: dict[str, tuple[Counter, Path]] = {}
        self.kpid = 1000
        self.closed = False

    def probe(self) -> BackendCapabilities:
        return BackendCapabilities("fake", True, "4.2.0", {}, {}, "aarch64", "6.8", "ubuntu",
                                   "24.04", "fake", [], True, False, False, True, False, True,
                                   ["c-single", "c-threads"], ["TEST DOUBLE"])

    def assess(self, spec: WorkloadSpec) -> CompatibilityReport:
        return decide([], reference_class=spec.reference_class, platform_verified=True)

    def launch(self, spec: WorkloadSpec, session_dir: Path, deadline: float) -> BackendSession:
        if self.mode == "refuse-launch":
            raise BackendError(ErrorCode.PERMISSION_DENIED, "refusing to start a DMTCP coordinator")
        report = Path(spec.argv[spec.argv.index("--report") + 1])
        roles = ("main", "t0", "t1", "t2") if "--threads" in spec.argv else ("main",)
        session_dir.mkdir(mode=0o700, parents=True)
        ckpt = session_dir / "dmtcp-ckpt"
        ckpt.mkdir(mode=0o700)
        self.kpid += 1
        worker = Counter(report, roles, secrets.token_hex(16), dict.fromkeys(roles, 0), self.kpid)
        worker.start()
        sid = uuid.uuid4().hex
        self.sessions[sid] = (worker, ckpt)
        return BackendSession(sid, self.kpid, 1, self.kpid, [],
                              {"checkpoint_dir": str(ckpt), "report": str(report)})

    def checkpoint(self, session: BackendSession, staging_dir: Path, options: CheckpointOptions,
                   deadline: float) -> CaptureResult:
        worker, ckpt = self.sessions[session.session_id]
        image = ckpt / "ckpt_fake.dmtcp"
        image.write_text(json.dumps({"nonce": worker.nonce, "counters": worker.snapshot(),
                                     "roles": list(worker.roles), "kpid": worker.kpid}),
                         encoding="utf-8")
        image.chmod(0o600)
        entries = artifacts.inspect_all(ckpt, [(image.name, ArtifactRole.IMAGE)])
        return CaptureResult(entries, {"expected_processes": 1, "filesystem": "ext4"}, 5, "")

    def capture_record(self, capture: CaptureResult) -> CaptureRecord:
        return CaptureRecord("dmtcp", "4.2.0", {"fake_tool": "1" * 64}, "aarch64", "6.8",
                             list(capture.artifacts), dict(capture.restore_hints))

    def stop(self, session: BackendSession) -> bool:
        self.sessions[session.session_id][0].stop()
        return True

    def restore(self, capture: CaptureRecord, artifact_dir: Path, options: RestoreOptions,
                deadline: float) -> BackendSession:
        artifacts.verify(artifact_dir, capture.artifacts)
        image = artifact_dir / capture.artifacts[0].relative_path
        state = json.loads(image.read_text(encoding="utf-8"))
        report = next(w.report for w, d in self.sessions.values() if d == artifact_dir)
        roles = tuple(state["roles"])
        nonce = secrets.token_hex(16) if self.mode == "fresh" else state["nonce"]
        counters = dict.fromkeys(roles, 0) if self.mode == "fresh" else state["counters"]
        self.kpid += 1
        worker = Counter(report, roles, nonce, counters, self.kpid,
                         die_after=0.2 if self.mode == "dies" else None,
                         inflight_kpid=state["kpid"] if self.mode == "inflight" else None)
        worker.start()
        if self.mode == "stray-writer":  # not this restore's process, same memory image
            stray = Counter(report, roles, nonce, counters, 9999)
            stray.start()
            self.sessions[uuid.uuid4().hex] = (stray, options.session_dir)
        options.session_dir.mkdir(mode=0o700, parents=True)
        sid = uuid.uuid4().hex
        self.sessions[sid] = (worker, options.session_dir)
        return BackendSession(sid, self.kpid, 1, self.kpid, [], {"checkpoint_dir": ""})

    def status(self, session: BackendSession) -> SessionStatus:
        alive = self.sessions[session.session_id][0].is_alive()
        return SessionStatus(SessionState.ALIVE if alive else SessionState.EXITED)

    def audit(self, session: BackendSession, step: str = "audit") -> None:
        return None

    def adopt_restored(self, session: BackendSession, pid: int) -> bool:
        return pid == session.pid

    def restore_diagnostics(self, session: BackendSession) -> dict[str, Any]:
        return {"test_double": True}

    def network_notes(self, session: BackendSession) -> list[dict[str, Any]]:
        return []

    def cleanup(self, session: BackendSession | None, path: Path | None) -> list[str]:
        if session is not None and session.session_id in self.sessions:
            self.sessions[session.session_id][0].stop()
        return []

    def leak_scan(self, path: Path) -> list[str]:
        return []

    def close_all(self) -> list[str]:
        self.closed = True
        for worker, _ in self.sessions.values():
            worker.stop()
        return []


def fake_build(work: Path) -> Path:
    exe = work / "bin" / "ngcounter"
    exe.parent.mkdir(mode=0o700, exist_ok=True)
    exe.write_bytes(b"test double")
    return exe
