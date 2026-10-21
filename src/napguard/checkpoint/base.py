"""Backend-neutral checkpoint contract (Stage 01 design, Stage 03a implementation).

See docs/architecture/checkpoint-backend.md. Stage 03a implements the contract
synchronously: every call takes an explicit monotonic `deadline` (REL-008), and the
Stage 06 daemon is expected to run calls off its event loop. Nothing here talks to
Linux; implementations live in `dmtcp.py` and `criu.py`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from .compatibility import CompatibilityReport


class ErrorCode(StrEnum):
    """Backend error codes: the backend contract plus the IPC storage/trust codes."""

    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    UNSUPPORTED_WORKLOAD = "UNSUPPORTED_WORKLOAD"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    LAUNCH_FAILED = "LAUNCH_FAILED"
    CHECKPOINT_FAILED = "CHECKPOINT_FAILED"
    RESTORE_FAILED = "RESTORE_FAILED"
    VERIFY_FAILED = "VERIFY_FAILED"
    RESOURCE_CONFLICT = "RESOURCE_CONFLICT"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    ARCH_MISMATCH = "ARCH_MISMATCH"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    INVALID_ARTIFACTS = "INVALID_ARTIFACTS"
    INVALID_MANIFEST = "INVALID_MANIFEST"
    INSECURE_ARTIFACT = "INSECURE_ARTIFACT"
    CORRUPT = "CORRUPT"
    INTERNAL = "INTERNAL"


class BackendError(Exception):
    def __init__(self, code: ErrorCode, message: str, *, retryable: bool = False,
                 detail: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable
        self.detail = detail or {}

    def to_json(self) -> dict[str, Any]:
        return {"code": str(self.code), "message": self.message, "retryable": self.retryable,
                "detail": self.detail}


def remaining(deadline: float, what: str) -> float:
    """Seconds left before `deadline` (time.monotonic()); TIMED_OUT if none are left."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise BackendError(ErrorCode.TIMED_OUT, f"deadline passed before {what}", retryable=True)
    return left


@dataclass(frozen=True)
class BackendCapabilities:
    name: str
    available: bool
    version: str | None
    tool_paths: dict[str, str]
    tool_sha256: dict[str, str]
    arch: str
    kernel_release: str
    os_id: str
    os_version_id: str
    self_check: str  # PLT-009: what was checked, in words
    requires_privileges: list[str]
    supports_leave_running: bool
    supports_cancel: bool
    supports_process_tree: bool
    supports_threads: bool
    restores_same_pid: bool
    verified: bool  # True only on the Stage 02-verified (backend, version, platform)
    verified_classes: list[str]
    notes: list[str] = field(default_factory=list)
    source_commit: str | None = None  # pinned build revision, when recorded


@dataclass(frozen=True)
class WorkloadSpec:
    workload_id: str
    executable: Path
    argv: list[str]  # argv[0] included
    working_dir: Path
    env: dict[str, str]  # already filtered (SEC-008)
    reference_class: str | None = None  # set only for NapGuard's own reference workloads


class SessionState(StrEnum):
    ALIVE = "ALIVE"
    EXITED = "EXITED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SessionStatus:
    state: SessionState
    exit_code: int | None = None
    signal: int | None = None
    reason: str = ""


@dataclass
class BackendSession:
    session_id: str
    pid: int
    pid_start_ticks: int
    pgid: int
    helper_pids: list[int]
    data: dict[str, Any] = field(default_factory=dict)  # JSON-serializable


@dataclass(frozen=True)
class CheckpointOptions:
    leave_running: bool = True  # FR-011
    include_children: bool = True


class ArtifactRole(StrEnum):
    IMAGE = "IMAGE"
    RESTART_SCRIPT = "RESTART_SCRIPT"  # evidence only: NapGuard never executes it
    METADATA = "METADATA"


@dataclass(frozen=True)
class ArtifactEntry:
    relative_path: str
    role: ArtifactRole
    size: int
    sha256: str


@dataclass(frozen=True)
class CaptureResult:
    artifacts: list[ArtifactEntry]
    restore_hints: dict[str, Any]
    capture_ms: int
    backend_log_excerpt: str


@dataclass(frozen=True)
class CaptureRecord:
    """The part of a manifest the backend needs to restore (CMP-004, CMP-005)."""

    backend: str
    backend_version: str
    tool_sha256: dict[str, str]
    arch: str
    kernel_release: str
    artifacts: list[ArtifactEntry]
    restore_hints: dict[str, Any]


@dataclass(frozen=True)
class RestoreOptions:
    session_dir: Path  # Stage 03a: where the restore keeps its coordinator, logs, and tmp
    working_dir: Path | None = None


class CancelOutcome(StrEnum):
    CANCELLED = "CANCELLED"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    TOO_LATE = "TOO_LATE"


class CheckpointBackend(Protocol):
    name: str

    def probe(self) -> BackendCapabilities: ...

    def assess(self, spec: WorkloadSpec) -> CompatibilityReport: ...

    def launch(self, spec: WorkloadSpec, session_dir: Path, deadline: float) -> BackendSession: ...

    def status(self, session: BackendSession) -> SessionStatus: ...

    def checkpoint(self, session: BackendSession, staging_dir: Path, options: CheckpointOptions,
                   deadline: float) -> CaptureResult: ...

    def restore(self, capture: CaptureRecord, artifact_dir: Path, options: RestoreOptions,
                deadline: float) -> BackendSession: ...

    def cancel(self, session: BackendSession) -> CancelOutcome: ...

    def cleanup(self, session: BackendSession | None, path: Path | None) -> list[str]: ...
