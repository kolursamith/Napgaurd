"""CRIU detection only (OD-14; ADR 0005 and ADR 0006 §6).

CRIU 4.2.1 needs CAP_CHECKPOINT_RESTORE and CAP_SYS_PTRACE, or root, to dump and
restore as an ordinary user under Ubuntu's Yama ptrace_scope=1. Granting either is
root-equivalent and conflicts with SEC-010, so Stage 03a only reports what is installed.
It runs `criu --version` and reads file capabilities with `getcap`; it never runs
`criu check`, `dump`, or `restore`, never uses sudo, and grants nothing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from napguard.core.proc import filtered_env

from .base import (
    BackendCapabilities,
    BackendError,
    BackendSession,
    CancelOutcome,
    CaptureRecord,
    CaptureResult,
    CheckpointOptions,
    ErrorCode,
    RestoreOptions,
    SessionStatus,
    WorkloadSpec,
)
from .compatibility import Compatibility, CompatibilityReport
from .dmtcp import host_platform, sha256_file

REQUIRED_CAPS = ("cap_checkpoint_restore", "cap_sys_ptrace")
PROBE_ONLY = ("CRIU is probe-only in Stage 03a: its privilege requirement (OD-14) is "
              "undecided, and NapGuard grants no capabilities (SEC-010)")


def parse_getcap(output: str) -> set[str]:
    """Capability names from `getcap <file>` (`/x/criu cap_a,cap_b=ep` or `cap_a+eip`)."""
    caps: set[str] = set()
    for line in output.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            for item in parts[1].replace(" ", ",").split(","):
                name = item.split("=")[0].split("+")[0].strip().lower()
                if name.startswith("cap_"):
                    caps.add(name)
    return caps


def _run(argv: list[str]) -> str | None:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=15,
                              env=filtered_env(), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return (done.stdout + done.stderr).strip() if done.returncode == 0 else None


class CriuBackend:
    name = "criu"

    def __init__(self, binary: Path | None = None) -> None:
        found = str(binary) if binary is not None and binary.is_file() else (
            None if binary is not None else shutil.which("criu"))
        self.binary = str(Path(found).resolve()) if found else None

    def probe(self) -> BackendCapabilities:
        """Version and file capabilities only. Runs no checkpoint and no `criu check`."""
        platform = host_platform()
        notes = [PROBE_ONLY]
        version = None
        caps: set[str] = set()
        if self.binary is None:
            notes.append("criu not found")
        else:
            text = _run([self.binary, "--version"])
            version = text.splitlines()[0].split()[-1] if text else None
            getcap = shutil.which("getcap", path="/usr/sbin:/usr/bin:/sbin:/bin")
            out = _run([getcap, self.binary]) if getcap else None
            caps = parse_getcap(out or "")
            notes.append(f"file capabilities: {sorted(caps) or 'none'}")
        return BackendCapabilities(
            name=self.name, available=False, version=version,
            tool_paths={"criu": self.binary} if self.binary else {},
            tool_sha256={"criu": sha256_file(self.binary)} if self.binary else {},
            arch=platform["arch"], kernel_release=platform["kernel_release"],
            os_id=platform["os_id"], os_version_id=platform["os_version_id"],
            self_check="not run: `criu check` is not executed in Stage 03a",
            requires_privileges=["CAP_CHECKPOINT_RESTORE and CAP_SYS_PTRACE, or root"],
            supports_leave_running=True, supports_cancel=False, supports_process_tree=True,
            supports_threads=True, restores_same_pid=True, verified=False,
            verified_classes=[], notes=notes)

    def assess(self, spec: WorkloadSpec) -> CompatibilityReport:
        return CompatibilityReport(Compatibility.UNKNOWN, notes=[PROBE_ONLY])

    def _refuse(self) -> BackendError:
        return BackendError(ErrorCode.PERMISSION_DENIED, PROBE_ONLY)

    def launch(self, spec: WorkloadSpec, session_dir: Path, deadline: float) -> BackendSession:
        raise self._refuse()

    def status(self, session: BackendSession) -> SessionStatus:
        raise self._refuse()

    def checkpoint(self, session: BackendSession, staging_dir: Path, options: CheckpointOptions,
                   deadline: float) -> CaptureResult:
        raise self._refuse()

    def restore(self, capture: CaptureRecord, artifact_dir: Path, options: RestoreOptions,
                deadline: float) -> BackendSession:
        raise self._refuse()

    def cancel(self, session: BackendSession) -> CancelOutcome:
        return CancelOutcome.NOT_SUPPORTED

    def cleanup(self, session: BackendSession | None, path: Path | None) -> list[str]:
        return []
