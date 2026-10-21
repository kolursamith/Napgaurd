"""Stage 03a self-test: a real checkpoint and restore THROUGH NapGuard's backend API.

Launch the reference workload under the backend, checkpoint it (verified completion),
let it run on, terminate it, restore from the checkpoint, and verify continuity: the
original nonce, a counter rolled back into the checkpoint window, progress, intact
digests and ballast, a restored process that this restore owns and that stays alive,
and full cleanup. Any missing step is FAIL or BLOCKED, never PASS.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from napguard.checkpoint.artifacts import PERSISTENT_FS
from napguard.checkpoint.base import (
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
from napguard.checkpoint.compatibility import Compatibility, CompatibilityReport
from napguard.checkpoint.manifest import read_manifest, write_manifest
from napguard.core.proc import filtered_env

from . import model
from .verify import (
    Check,
    Marks,
    Status,
    first_report_latency_s,
    overall,
    ownership_problems,
    verify_restore,
)

TICK_MS = 10
SOURCE = Path(__file__).with_name("ngcounter.c")


@dataclass(frozen=True)
class ReferenceClass:
    args: tuple[str, ...]
    roles: tuple[str, ...]
    ballast_mib: int = 0
    report_every: int = 10


# Only Stage 02-verified, single-process reference classes without held files.
CLASSES = {
    "c-single": ReferenceClass(("--ballast-mib", "4"), ("main",), ballast_mib=4),
    "c-threads": ReferenceClass(("--threads", "3"), ("main", "t0", "t1", "t2")),
}


class SelftestBackend(Protocol):
    def probe(self) -> BackendCapabilities: ...
    def assess(self, spec: WorkloadSpec) -> CompatibilityReport: ...
    def launch(self, spec: WorkloadSpec, session_dir: Path, deadline: float) -> BackendSession: ...
    def checkpoint(self, session: BackendSession, staging_dir: Path, options: CheckpointOptions,
                   deadline: float) -> CaptureResult: ...
    def capture_record(self, capture: CaptureResult) -> CaptureRecord: ...
    def stop(self, session: BackendSession) -> bool: ...
    def restore(self, capture: CaptureRecord, artifact_dir: Path, options: RestoreOptions,
                deadline: float) -> BackendSession: ...
    def status(self, session: BackendSession) -> SessionStatus: ...
    def audit(self, session: BackendSession, step: str = ...) -> None: ...
    def adopt_restored(self, session: BackendSession, pid: int) -> bool: ...
    def restore_diagnostics(self, session: BackendSession) -> dict[str, Any]: ...
    def network_notes(self, session: BackendSession) -> list[dict[str, Any]]: ...
    def cleanup(self, session: BackendSession | None, path: Path | None) -> list[str]: ...
    def leak_scan(self, path: Path) -> list[str]: ...


@dataclass
class SelftestOptions:
    work_dir: Path  # must not exist yet; created with mode 0700
    workload_class: str = "c-single"
    run_on_s: float = 4.0
    observe_s: float = 3.0
    verify_window_s: float = 3.0
    step_timeout_s: float = 60.0
    report_timeout_s: float = 30.0


@dataclass
class SelftestResult:
    status: Status
    checks: list[Check]
    facts: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"status": str(self.status), "checks": [c.to_json() for c in self.checks],
                "facts": self.facts}


class Blocked(Exception):
    pass


def build_reference(work_dir: Path) -> Path:
    """Compile the packaged reference workload with the system C compiler."""
    cc = shutil.which("cc")
    if cc is None:
        raise Blocked("no C compiler (`cc`) found; install build-essential")
    out = work_dir / "bin" / "ngcounter"
    out.parent.mkdir(mode=0o700, exist_ok=True)
    done = subprocess.run([cc, "-O2", "-Wall", "-Wextra", "-pthread", "-o", str(out),
                           str(SOURCE)], capture_output=True, text=True, timeout=120,
                          env=filtered_env(), stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        raise Blocked(f"building the reference workload failed: {done.stderr.strip()[-500:]}")
    return out


def _by_role(observations: Sequence[model.Observation]) -> dict[str, list[model.Observation]]:
    grouped: dict[str, list[model.Observation]] = {}
    for obs in observations:
        grouped.setdefault(obs.role, []).append(obs)
    return grouped


def _classify(exc: BackendError) -> Status:
    """Refusals before anything ran are BLOCKED; failures and exposure are FAIL."""
    exposure = ("network exposure" in exc.message or "beyond loopback" in exc.message)
    if exc.code is ErrorCode.BACKEND_UNAVAILABLE:
        return Status.BLOCKED
    if exc.code is ErrorCode.PERMISSION_DENIED and not exposure:
        return Status.BLOCKED
    return Status.FAIL


class _Run:
    def __init__(self, backend: SelftestBackend, opts: SelftestOptions,
                 sleep: Callable[[float], None]) -> None:
        self.backend = backend
        self.opts = opts
        self.sleep = sleep
        self.checks: list[Check] = []
        self.facts: dict[str, Any] = {"workload_class": opts.workload_class,
                                      "work_dir": str(opts.work_dir)}
        self.sessions: list[BackendSession] = []
        self.report = opts.work_dir / "reports" / "managed.txt"
        self.cls = CLASSES[opts.workload_class]

    def add(self, name: str, status: Status, detail: str, **facts: object) -> Status:
        self.checks.append(Check(name, status, detail, dict(facts)))
        return status

    def deadline(self) -> float:
        return time.monotonic() + self.opts.step_timeout_s

    def wait_reports(self, start_line: int, count: int, session: BackendSession,
                     step: str) -> bool:
        """Poll the report file; audit the network on every poll (ADR 0006 §1)."""
        end = time.monotonic() + self.opts.report_timeout_s
        while True:
            grouped = _by_role(model.read_reports(self.report, start_line))
            if all(len(grouped.get(r, [])) >= count for r in self.cls.roles):
                return True
            self.backend.audit(session, step)
            if time.monotonic() >= end:
                return False
            self.sleep(0.25)

    def spec(self, binary: Path) -> WorkloadSpec:
        argv = [str(binary), "--report", str(self.report), "--label", "managed",
                "--tick-ms", str(TICK_MS), "--report-every", str(self.cls.report_every),
                *self.cls.args]
        return WorkloadSpec(f"selftest-{self.opts.workload_class}", binary, argv,
                            self.opts.work_dir, filtered_env(),
                            reference_class=self.opts.workload_class)

    def steps(self, build: Callable[[Path], Path]) -> None:
        caps = self.backend.probe()
        self.facts["backend"] = {"name": caps.name, "version": caps.version,
                                 "arch": caps.arch, "os": f"{caps.os_id} {caps.os_version_id}",
                                 "verified": caps.verified, "tool_sha256": caps.tool_sha256}
        if not caps.available:
            self.add("backend available", Status.BLOCKED, "; ".join(caps.notes))
            return
        self.add("backend available", Status.PASS, f"{caps.name} {caps.version}")
        binary = build(self.opts.work_dir)
        spec = self.spec(binary)
        assessed = self.backend.assess(spec)
        self.facts["assessment"] = assessed.to_json()
        if assessed.level is not Compatibility.VERIFIED:
            status = Status.FAIL if assessed.refused else Status.BLOCKED
            self.add("reference workload assessed", status,
                     f"{assessed.level}: the self-test runs only on the Stage 02-verified platform")
            return
        self.add("reference workload assessed", Status.PASS, "VERIFIED reference class")

        session = self.backend.launch(spec, self.opts.work_dir / "session-launch", self.deadline())
        self.sessions.append(session)
        if not self.wait_reports(0, 3, session, "launch"):
            self.add("managed workload running", Status.FAIL,
                     f"no 3 reports from every role within {self.opts.report_timeout_s:.0f} s")
            return
        started = model.read_reports(self.report)
        self.add("managed workload running", Status.PASS,
                 f"roles {list(self.cls.roles)} reporting; nonce {started[0].nonce}",
                 nonce=started[0].nonce, pid=session.pid)

        lower = {r: max(o.counter for o in obs)
                 for r, obs in _by_role(model.read_reports(self.report)).items()}
        capture = self.backend.checkpoint(session, Path(session.data["checkpoint_dir"]),
                                          CheckpointOptions(), self.deadline())
        images = [a for a in capture.artifacts if a.role.value == "IMAGE"]
        self.facts["capture"] = {"capture_ms": capture.capture_ms,
                                 "artifacts": [vars(a) for a in capture.artifacts]}
        self.add("checkpoint verified complete", Status.PASS,
                 f"{len(images)} image(s), {sum(a.size for a in images)} bytes, "
                 f"{capture.capture_ms} ms (completion verified, never by exit status)")
        fs = str(capture.restore_hints.get("filesystem", "?"))
        if self.add("artifacts on persistent filesystem",
                    Status.PASS if fs in PERSISTENT_FS else Status.FAIL,
                    f"checkpoint directory is on {fs} (PLT-007: never tmpfs/ramfs)") \
                is not Status.PASS:
            return
        ckpt_dir = Path(session.data["checkpoint_dir"])
        manifest = write_manifest(ckpt_dir, self.backend.capture_record(capture))
        self.facts["manifest"] = str(manifest)
        done_line = model.count_lines(self.report)
        if not self.wait_reports(done_line, 1, session, "after checkpoint"):
            self.add("workload continued after checkpoint", Status.FAIL,
                     "no new report after the checkpoint (FR-011)")
            return
        upper = {r: min(o.counter for o in obs)
                 for r, obs in _by_role(model.read_reports(self.report, done_line)).items()}
        self.add("workload continued after checkpoint", Status.PASS,
                 f"first post-checkpoint counters {upper} (FR-011)")

        self.sleep(self.opts.run_on_s)
        stopped = self.backend.stop(session)
        self.sleep(0.3)  # nothing below can be output of the original
        state = self.backend.status(session)
        original_dead = stopped and state.state is not SessionState.ALIVE
        original = model.read_reports(self.report)
        kill = {r: max(o.counter for o in obs) for r, obs in _by_role(original).items()}
        kill_line = model.count_lines(self.report)
        self.add("original terminated", Status.PASS if original_dead else Status.FAIL,
                 f"counters at termination {kill}; status {state.state}")
        if not original_dead:
            return

        record = read_manifest(ckpt_dir)  # INVALID_MANIFEST / CORRUPT refuse the restore
        self.add("manifest validated", Status.PASS,
                 f"{len(record.artifacts)} artifact(s); digest, schema, names and limitation "
                 "statements (LIM-007: external files are not rolled back) checked")
        started_ns = time.monotonic_ns()
        restored_session = self.backend.restore(
            record, ckpt_dir,
            RestoreOptions(session_dir=self.opts.work_dir / "session-restore",
                           working_dir=self.opts.work_dir), self.deadline())
        self.sessions.append(restored_session)
        self.add("restore started", Status.PASS,
                 "restore command running; restoration NOT yet verified")
        if not self.wait_reports(kill_line, 3, restored_session, "restore (waiting)"):
            self.facts["restore_diagnostics"] = self.backend.restore_diagnostics(restored_session)
            self.add("restored workload reporting", Status.FAIL,
                     f"{len(model.read_reports(self.report, kill_line))} report(s) within "
                     f"{self.opts.report_timeout_s:.0f} s of the restore")
            return
        early = model.read_reports(self.report, kill_line)
        kpids = sorted({o.kpid for o in early if o.kpid is not None})
        problems = ownership_problems(
            early, original_kpids={o.kpid for o in original if o.kpid is not None},
            adopt=lambda k: self.backend.adopt_restored(restored_session, k))
        self.add("restored process owned by this restore",
                 Status.FAIL if problems else Status.PASS,
                 f"kernel pids {kpids} "
                 + ("NOT adoptable: not this restore's process: " + "; ".join(problems[:3])
                    if problems else "adopted (this user, this restore's session, started after "
                    "the restore); at most one in-flight report per role from the dead original"))
        if problems:
            return

        end = time.monotonic() + self.opts.verify_window_s
        alive = True
        while time.monotonic() < end and alive:
            alive = self.backend.status(restored_session).state is SessionState.ALIVE
            self.backend.audit(restored_session, "verification window")
            self.sleep(0.25)
        self.add("restored process stayed alive", Status.PASS if alive else Status.FAIL,
                 f"alive for the {self.opts.verify_window_s:.0f} s verification window (FR-026)"
                 if alive else "the restored process exited during the verification window")
        if not alive:
            self.facts["restore_diagnostics"] = self.backend.restore_diagnostics(restored_session)
            return

        self.sleep(self.opts.observe_s)
        restored = model.read_reports(self.report, kill_line)
        self.facts["restore_to_first_report_s"] = first_report_latency_s(started_ns, restored)
        for role in self.cls.roles:
            check = verify_restore(role=role, original=original,
                                   marks=Marks(lower[role], upper[role], kill[role]),
                                   restored=restored, original_dead=original_dead,
                                   ballast_mib=self.cls.ballast_mib)
            self.checks.append(check)

    def finish(self) -> None:
        problems: list[str] = []
        self.facts["host_sockets_seen"] = [n for s in self.sessions
                                           for n in self.backend.network_notes(s)][:50]
        for session in reversed(self.sessions):
            try:
                problems += self.backend.cleanup(session, None)
            except BackendError as exc:
                problems.append(str(exc))
        problems += [f"leaked {p}" for p in self.backend.leak_scan(self.opts.work_dir)]
        self.add("processes cleaned up", Status.FAIL if problems else Status.PASS,
                 "; ".join(problems) if problems else
                 "every session process stopped; no process references the work directory")


def run_selftest(backend: SelftestBackend, opts: SelftestOptions, *,
                 build: Callable[[Path], Path] = build_reference,
                 sleep: Callable[[float], None] = time.sleep) -> SelftestResult:
    if opts.workload_class not in CLASSES:
        raise ValueError(f"unknown self-test class {opts.workload_class!r}")
    opts.work_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    (opts.work_dir / "reports").mkdir(mode=0o700)
    run = _Run(backend, opts, sleep)
    try:
        run.steps(build)
    except Blocked as exc:
        run.add("prerequisites", Status.BLOCKED, str(exc))
    except model.MalformedReport as exc:  # a finished record that is corrupt: never ignored
        run.facts["malformed_report"] = str(exc)
        run.add("reports well-formed", Status.FAIL, f"corrupt workload report: {exc}")
    except BackendError as exc:
        run.facts["error"] = exc.to_json()
        run.add(f"backend call ({exc.code})", _classify(exc), exc.message)
    finally:
        run.finish()
    result = SelftestResult(overall(run.checks), run.checks, run.facts)
    path = opts.work_dir / "selftest-result.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(result.to_json(), fh, indent=2, sort_keys=True, default=str)
    return result
