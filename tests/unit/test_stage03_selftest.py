"""Stage 03a self-test and continuity verifier, driven through a FAKE backend.

The fake backend runs an in-memory counter in a thread that writes the same NGSTATE
reports as the C reference workload. It is a labelled test double: it proves the
self-test's decision logic, not that DMTCP works (that needs the Ubuntu run).
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from napguard.checkpoint.base import (
    ArtifactEntry,
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
from napguard.selftest import model, verify
from napguard.selftest.run import CLASSES, SelftestOptions, SelftestResult, run_selftest
from napguard.selftest.verify import Status

ROLES = CLASSES["c-threads"].roles


class FakeWorkload(threading.Thread):
    def __init__(self, report: Path, nonce: str, counters: dict[str, int], kpid: int,
                 die_after: float | None = None) -> None:
        super().__init__(daemon=True)
        self.report, self.nonce, self.kpid = report, nonce, kpid
        self.counters = dict(counters)
        self.chains = {r: model.DigestChain(nonce, r) for r in ROLES}
        self.halt = threading.Event()
        self.die_at = time.monotonic() + die_after if die_after is not None else None
        self.lock = threading.Lock()

    def run(self) -> None:
        while not self.halt.is_set():
            if self.die_at is not None and time.monotonic() >= self.die_at:
                return
            lines = []
            with self.lock:
                for role in ROLES:
                    self.counters[role] += 1
                    c = self.counters[role]
                    if c % 10 == 0:
                        lines.append(
                            f"NGSTATE v1 run=managed role={role} pid=1 kpid={self.kpid} "
                            f"nonce={self.nonce} counter={c} digest={self.chains[role].at(c):016x} "
                            f"ballast=- mono_ns={time.monotonic_ns()}\n")
            if lines:
                with self.report.open("a", encoding="utf-8") as fh:
                    fh.writelines(lines)
            time.sleep(0.001)

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return dict(self.counters)

    def stop(self) -> None:
        self.halt.set()
        self.join(5)


class FakeBackend:
    """Modes: genuine, fresh, no-rollback, survivor, foreign, dies, silent, restore-fails,
    checkpoint-fails, manifest-tamper, tmpfs, exposure, unavailable."""

    def __init__(self, mode: str = "genuine") -> None:
        self.mode = mode
        self.workloads: dict[str, FakeWorkload] = {}
        self.snapshot: dict[str, int] = {}
        self.nonce = ""
        self.report: Path | None = None
        self.cleaned: list[str] = []

    def probe(self) -> BackendCapabilities:
        ok = self.mode != "unavailable"
        return BackendCapabilities("fake", ok, "4.2.0", {}, {}, "aarch64", "6.8", "ubuntu",
                                   "24.04", "fake", [], True, False, False, True, False, ok,
                                   [], [] if ok else ["fake backend unavailable"])

    def assess(self, spec: WorkloadSpec) -> CompatibilityReport:
        return decide([], reference_class=spec.reference_class, platform_verified=True)

    def _session(self, sid: str, kpid: int, d: Path) -> BackendSession:
        return BackendSession(sid, kpid, 1, kpid, [], {"checkpoint_dir": str(d)})

    def launch(self, spec: WorkloadSpec, session_dir: Path, deadline: float) -> BackendSession:
        self.report = Path(spec.argv[spec.argv.index("--report") + 1])
        session_dir.mkdir(mode=0o700)
        self.ckpt = session_dir
        self.nonce = secrets.token_hex(16)
        wl = FakeWorkload(self.report, self.nonce, dict.fromkeys(ROLES, 0), 1001)
        self.workloads["launch"] = wl
        wl.start()
        return self._session("launch", 1001, session_dir)

    def checkpoint(self, session: BackendSession, staging_dir: Path, options: CheckpointOptions,
                   deadline: float) -> CaptureResult:
        if self.mode == "checkpoint-fails":
            raise BackendError(ErrorCode.CHECKPOINT_FAILED, "fake checkpoint failure")
        self.snapshot = self.workloads["launch"].snapshot()
        fs = "tmpfs" if self.mode == "tmpfs" else "ext4"
        return CaptureResult([ArtifactEntry("ckpt.dmtcp", ArtifactRole.IMAGE, 9, "0" * 64)],
                             {"expected_processes": 1, "filesystem": fs}, 7, "")

    def capture_record(self, capture: CaptureResult) -> CaptureRecord:
        return CaptureRecord("fake", "4.2.0", {"fake_tool": "1" * 64}, "aarch64", "6.8",
                             list(capture.artifacts), dict(capture.restore_hints))

    def stop(self, session: BackendSession) -> bool:
        if self.mode == "survivor":
            return False  # the original could not be confirmed dead
        if self.mode == "manifest-tamper":
            path = self.ckpt / "napguard-manifest.json"
            path.write_text(path.read_text().replace('"size": 9', '"size": 10'))
        self.workloads["launch"].stop()
        return True

    def restore(self, capture: CaptureRecord, artifact_dir: Path, options: RestoreOptions,
                deadline: float) -> BackendSession:
        assert self.report is not None
        if self.mode == "restore-fails":
            raise BackendError(ErrorCode.RESTORE_FAILED, "dmtcp_restart exited within 1 s",
                               detail={"restarter_exit": {"how": "CLD_DUMPED", "signal": 11}})
        if self.mode == "silent":  # the restore command runs, but nothing is restored
            wl = FakeWorkload(self.report.with_name("nowhere.txt"), self.nonce, self.snapshot,
                              2002)
        elif self.mode == "fresh":  # "restore" merely starts a new process
            wl = FakeWorkload(self.report, secrets.token_hex(16), dict.fromkeys(ROLES, 0), 2002)
        elif self.mode == "no-rollback":  # continues from termination, not from the checkpoint
            wl = FakeWorkload(self.report, self.nonce, self.workloads["launch"].snapshot(), 2002)
        else:
            wl = FakeWorkload(self.report, self.nonce, self.snapshot, 2002,
                              die_after=0.15 if self.mode == "dies" else None)
        self.workloads["restore"] = wl
        wl.start()
        return self._session("restore", 2002, options.session_dir)

    def status(self, session: BackendSession) -> SessionStatus:
        alive = self.workloads[session.session_id].is_alive()
        return SessionStatus(SessionState.ALIVE if alive else SessionState.EXITED)

    def audit(self, session: BackendSession, step: str = "audit") -> None:
        if self.mode == "exposure" and session.session_id == "restore":
            raise BackendError(ErrorCode.PERMISSION_DENIED, f"network exposure after {step}")

    def adopt_restored(self, session: BackendSession, pid: int) -> bool:
        return self.mode != "foreign" and pid == session.pid

    def restore_diagnostics(self, session: BackendSession) -> dict[str, Any]:
        return {"fake": True}

    def network_notes(self, session: BackendSession) -> list[dict[str, Any]]:
        return []

    def cleanup(self, session: BackendSession | None, path: Path | None) -> list[str]:
        if session is not None:
            self.workloads[session.session_id].stop()
            self.cleaned.append(session.session_id)
        return []

    def leak_scan(self, path: Path) -> list[str]:
        return [name for name, wl in self.workloads.items() if wl.is_alive()]


def selftest(tmp_path: Path, mode: str) -> tuple[SelftestResult, FakeBackend]:
    backend = FakeBackend(mode)
    opts = SelftestOptions(tmp_path / "run", "c-threads", run_on_s=0.4, observe_s=0.3,
                           verify_window_s=0.4, step_timeout_s=10, report_timeout_s=5)
    result = run_selftest(backend, opts, build=lambda d: d / "ngcounter")
    return result, backend


def statuses(result: SelftestResult) -> dict[str, Status]:
    return {c.name: c.status for c in result.checks}


def test_genuine_restore_passes_every_check(tmp_path: Path) -> None:
    result, backend = selftest(tmp_path, "genuine")
    assert result.status is Status.PASS, result.to_json()
    got = statuses(result)
    assert all(got[f"restored state continuity ({r})"] is Status.PASS for r in ROLES)
    assert got["original terminated"] is Status.PASS
    assert got["restored process owned by this restore"] is Status.PASS
    assert got["processes cleaned up"] is Status.PASS
    assert got["manifest validated"] is Status.PASS
    assert got["artifacts on persistent filesystem"] is Status.PASS
    assert backend.cleaned == ["restore", "launch"]
    saved = tmp_path / "run" / "selftest-result.json"
    assert saved.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(("mode", "check", "why"), [
    ("fresh", "restored state continuity (main)", "nonce changed"),
    ("no-rollback", "restored state continuity (main)", "did not roll back"),
    ("survivor", "original terminated", "status"),
    ("foreign", "restored process owned by this restore", "NOT adoptable"),
    ("dies", "restored process stayed alive", "exited"),
    ("silent", "restored workload reporting", "report(s) within"),
    ("tmpfs", "artifacts on persistent filesystem", "tmpfs"),
])
def test_no_false_pass(tmp_path: Path, mode: str, check: str, why: str) -> None:
    result, backend = selftest(tmp_path, mode)
    assert result.status is Status.FAIL
    failed = next(c for c in result.checks if c.name == check)
    assert failed.status is Status.FAIL and why in failed.detail
    assert statuses(result)["processes cleaned up"] is Status.PASS


@pytest.mark.parametrize(("mode", "code"), [("checkpoint-fails", "CHECKPOINT_FAILED"),
                                           ("restore-fails", "RESTORE_FAILED"),
                                           ("manifest-tamper", "INVALID_MANIFEST")])
def test_failed_checkpoint_restore_or_manifest_is_fail_and_cleans_up(
        tmp_path: Path, mode: str, code: str) -> None:
    result, backend = selftest(tmp_path, mode)
    assert result.status is Status.FAIL
    assert "launch" in backend.cleaned and backend.leak_scan(tmp_path) == []
    assert result.facts["error"]["code"] == code
    if mode == "manifest-tamper":
        assert "restore" not in backend.workloads  # an invalid manifest refuses the restore


def test_exposure_is_fail_and_unavailable_backend_is_blocked(tmp_path: Path) -> None:
    exposed, _ = selftest(tmp_path / "a", "exposure")
    assert exposed.status is Status.FAIL
    blocked, backend = selftest(tmp_path / "b", "unavailable")
    assert blocked.status is Status.BLOCKED and backend.workloads == {}


def test_verifier_requires_rollback_window_and_progress() -> None:
    nonce = "ab" * 16

    def obs(counter: int, line: int, n: str = nonce) -> model.Observation:
        return model.Observation("r", "main", 1, 1, n, counter, model.digest_at(n, "main", counter),
                                 None, 0, line)

    original = [obs(10, 1), obs(20, 2), obs(90, 3)]
    marks = verify.Marks(20, 30, 90)
    ok = verify.verify_restore(role="main", original=original, marks=marks,
                               restored=[obs(30, 4), obs(40, 5)], original_dead=True)
    assert ok.status is Status.PASS
    for restored, dead, reason in [
        ([obs(30, 4), obs(40, 5)], False, "not confirmed terminated"),
        ([obs(100, 4), obs(110, 5)], True, "did not roll back"),
        ([obs(10, 4), obs(20, 5)], True, "below the pre-checkpoint value"),
        ([obs(30, 4), obs(30, 5)], True, "did not advance"),
        ([obs(30, 4)], True, "fewer than two"),
    ]:
        check = verify.verify_restore(role="main", original=original, marks=marks,
                                      restored=restored, original_dead=dead)
        assert check.status is Status.FAIL and reason in check.detail
    zero = verify.verify_restore(role="main", original=original, marks=verify.Marks(0, 10, 90),
                                 restored=[obs(10, 4), obs(20, 5)], original_dead=True)
    assert zero.status is Status.FAIL  # a checkpoint at counter 0 proves nothing
    assert verify.overall([]) is Status.BLOCKED  # nothing executed is never PASS


def test_reference_source_is_the_stage02_verified_workload() -> None:
    root = Path(__file__).resolve().parents[2]
    packaged = root / "src" / "napguard" / "selftest" / "ngcounter.c"
    assert packaged.read_bytes() == (root / "experiments" / "stage02" / "workload"
                                     / "ngcounter.c").read_bytes()
    assert os.access(packaged, os.R_OK)
