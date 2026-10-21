"""DMTCP 4.2.0 backend adapter (Stage 03a, developer mode only; ADR 0006).

Reviewed port of the mechanisms verified on Ubuntu 24.04 aarch64 in Stage 02
(`experiments/stage02/ngexp/backends.py`):

* a private coordinator per session, started ONLY after `CoordinatorPolicy.authorize`,
  through the fail-closed loopback hand-off: a pre-bound 127.0.0.1 listener on fd 821 plus
  the same `--coord-port`, the kernel's wildcard-bind refusal checked first, the port file
  checked, then `ss` and IPv4/IPv6 reachability audits;
* every tool call joins that coordinator explicitly (`--join-coordinator`), so no tool
  ever starts its own coordinator;
* a checkpoint counts only after the verified completion wait (never an exit status);
* listener audits (baseline diff plus ownership) after every step and while waiting; an
  audit failure or exposure kills the session's processes and fails the call;
* the Stage 02 negative findings are refused before launch and before every checkpoint.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from napguard.core import proc
from napguard.core.netaudit import (
    AuditError,
    ListenerGuard,
    current_listeners,
    listener_violations,
    loopback_listener,
    non_loopback_ipv4_addresses,
    non_loopback_ipv6_addresses,
    tcp_reachable,
    wildcard_bind_refused,
)
from napguard.core.proc import ManagedProcess, ProcessTracker, SessionState

from . import artifacts
from .base import (
    ArtifactRole,
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
    remaining,
)
from .base import SessionState as RunState
from .compatibility import (
    VERIFIED_CLASSES,
    VERIFIED_PLATFORM,
    CompatibilityReport,
    Finding,
    ReasonCode,
    collect_runtime_facts,
    decide,
    runtime_findings,
    static_findings,
)
from .dmtcp_completion import parse_dmtcp_status, wait_for_checkpoint
from .policy import CoordinatorPolicy, single_user_host_findings

ANALYSED_VERSION = "4.2.0"  # the fd-821 hand-off was analysed for this version only
PINNED_COMMIT = "f8009ce7b4ad211311ca2f72a929b975e4aa1155"  # dmtcp tag v4.2.0 (ADR 0005)
BUILD_INFO = "napguard-build-info.txt"  # written by experiments/stage02/build-dmtcp.sh
PROTECTED_COORD_FD = 821  # PROTECTED_FD_START (820) + 1, include/protectedfds.h
PROTECTED_FDS = range(820, 860)  # DMTCP's own descriptors; below 820 belongs to the workload
LOOPBACK = "127.0.0.1"
CHECKPOINT_WAIT_CAP_S = 180.0
RESTORE_FAIL_FAST_S = 1.0
TOOL_NAMES = ("dmtcp_launch", "dmtcp_command", "dmtcp_coordinator", "dmtcp_restart")
BUILD_GUIDANCE = ("build DMTCP 4.2.0 from the pinned source with "
                  "experiments/stage02/build-dmtcp.sh, or pass --dmtcp-prefix")


@dataclass(frozen=True)
class DmtcpTools:
    launch: str
    command: str
    coordinator: str
    restart: str

    def paths(self) -> dict[str, str]:
        return dict(zip(TOOL_NAMES, (self.launch, self.command, self.coordinator, self.restart),
                        strict=True))


def default_prefixes() -> list[Path]:
    base = Path.home() / ".local" / "opt"
    found: list[Path] = []
    for parent in (base / "napguard", base / "napguard-stage02"):
        found.extend(sorted(parent.glob("dmtcp-*"), reverse=True))
    return found


def find_tools(prefix: Path | None, search_path: str | None = None) -> DmtcpTools | None:
    """Absolute, resolved tool paths (PLT-006). An explicit prefix is the only candidate."""
    candidates: list[Path | None] = [prefix] if prefix is not None else [*default_prefixes(), None]
    for base in candidates:
        if base is None:
            found = [shutil.which(n, path=search_path) for n in TOOL_NAMES]
        else:
            found = [str(base / "bin" / n) if (base / "bin" / n).is_file() else None
                     for n in TOOL_NAMES]
        if all(found):
            return DmtcpTools(*(str(Path(f).resolve()) for f in found if f))
    return None


def parse_version(text: str) -> str | None:
    """`dmtcp_launch (DMTCP) 4.2.0` -> "4.2.0"."""
    first = text.strip().splitlines()[0] if text.strip() else ""
    match = re.search(r"\(DMTCP\)\s+(\d+\.\d+\.\d+)\b", first) or re.search(
        r"\b(\d+\.\d+\.\d+)\b", first)
    return match.group(1) if match else None


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def build_commit(tools: DmtcpTools) -> str | None:
    """Source commit recorded by the pinned build script next to the tools, if any."""
    info = Path(tools.launch).parent.parent / BUILD_INFO
    try:
        for line in info.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "commit":
                return value.strip()
    except OSError:
        return None
    return None


def host_platform() -> dict[str, str]:
    release: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                release[key] = value.strip().strip('"')
    except OSError:
        pass
    return {"arch": platform.machine(), "kernel_release": platform.release(),
            "os_id": release.get("ID", "?"), "os_version_id": release.get("VERSION_ID", "?")}


# ------------------------------------------------------------ argument vectors


def coordinator_argv(tools: DmtcpTools, port_file: Path, ckpt_dir: Path, port: int) -> list[str]:
    # `port` is the pre-bound loopback listener's port, handed over on fd 821. The same port
    # makes any fallback INADDR_ANY bind fail with EADDRINUSE (fail-closed).
    return [tools.coordinator, "--coord-port", str(port), "--port-file", str(port_file),
            "--ckptdir", str(ckpt_dir)]


def launch_argv(tools: DmtcpTools, port: int, ckpt_dir: Path, tmp_dir: Path,
                workload: Sequence[str]) -> list[str]:
    return [tools.launch, "--coord-host", LOOPBACK, "--coord-port", str(port),
            "--join-coordinator", "--ckptdir", str(ckpt_dir), "--tmpdir", str(tmp_dir),
            *workload]


def command_argv(tools: DmtcpTools, port: int, *command: str) -> list[str]:
    return [tools.command, "--coord-host", LOOPBACK, "--coord-port", str(port), *command]


def restart_argv(tools: DmtcpTools, port: int, tmp_dir: Path, images: Sequence[Path]) -> list[str]:
    return [tools.restart, "--coord-host", LOOPBACK, "--coord-port", str(port),
            "--join-coordinator", "--tmpdir", str(tmp_dir), *map(str, images)]


# --------------------------------------------------------------------- adapter


@dataclass
class _Live:
    session_id: str
    kind: str  # "launch" or "restore"
    tracker: ProcessTracker
    guard: ListenerGuard
    session_dir: Path
    ckpt_dir: Path
    tmp_dir: Path
    coordinator: ManagedProcess
    listener: socket.socket
    port: int
    workload: ManagedProcess | None = None
    expected_images: int = 1
    notes: dict[str, Any] = field(default_factory=dict)


def make_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with artifacts.private_dir(path):
        pass  # raises INSECURE_ARTIFACT unless owned by us with mode 0700 (or stricter)
    return path


class DmtcpBackend:
    name = "dmtcp"

    def __init__(self, policy: CoordinatorPolicy | None = None, prefix: Path | None = None, *,
                 tools: DmtcpTools | None = None,
                 host_findings: Any = single_user_host_findings,
                 platform_facts: dict[str, str] | None = None) -> None:
        self.policy = policy if policy is not None else CoordinatorPolicy.deny()
        self.tools = tools if tools is not None else find_tools(prefix)
        self._host_findings = host_findings
        self.platform = platform_facts if platform_facts is not None else host_platform()
        self._version: str | None = None
        self._version_checked = False
        self._tool_sha: dict[str, str] = {}
        self._live: dict[str, _Live] = {}

    # ---------------------------------------------------------------- probe

    @property
    def version(self) -> str | None:
        if not self._version_checked and self.tools is not None:
            self._version_checked = True
            try:
                done = subprocess.run([self.tools.launch, "--version"], capture_output=True,
                                      text=True, timeout=15, env=proc.filtered_env(),
                                      stdin=subprocess.DEVNULL)
                self._version = parse_version(done.stdout + "\n" + done.stderr) \
                    if done.returncode == 0 else None
            except (OSError, subprocess.SubprocessError):
                self._version = None
        return self._version

    def tool_sha256(self) -> dict[str, str]:
        if not self._tool_sha and self.tools is not None:
            self._tool_sha = {n: sha256_file(p) for n, p in self.tools.paths().items()}
        return dict(self._tool_sha)

    def source_commit(self) -> str | None:
        return build_commit(self.tools) if self.tools is not None else None

    def platform_verified(self) -> bool:
        p = self.platform
        return (self.version == VERIFIED_PLATFORM["version"]
                and self.source_commit() == PINNED_COMMIT
                and p.get("arch") == VERIFIED_PLATFORM["arch"]
                and p.get("os_id") == VERIFIED_PLATFORM["os_id"]
                and p.get("os_version_id") == VERIFIED_PLATFORM["os_version_id"])

    def probe(self) -> BackendCapabilities:
        """Tools, version, and platform. Starts no coordinator and no workload."""
        notes: list[str] = []
        version = self.version
        if self.tools is None:
            notes.append(f"DMTCP tools not found: {BUILD_GUIDANCE}")
        elif version != ANALYSED_VERSION:
            notes.append(f"DMTCP version {version or 'unknown'} is not the analysed "
                         f"{ANALYSED_VERSION} (UNANALYSED_VERSION)")
        available = self.tools is not None and version == ANALYSED_VERSION
        verified = available and self.platform_verified()
        if available and self.source_commit() != PINNED_COMMIT:
            notes.append(f"DMTCP source commit {self.source_commit() or 'unknown'} is not the "
                         f"pinned {PINNED_COMMIT} ({BUILD_INFO} missing or different): "
                         "workloads are UNKNOWN at best")
        elif available and not verified:
            notes.append("platform differs from the Stage 02-verified Ubuntu 24.04 aarch64: "
                         "workloads are UNKNOWN at best")
        notes.append("coordinators run only in Stage 03a developer mode (ADR 0006); "
                     "not production-approved")
        return BackendCapabilities(
            name=self.name, available=available, version=version,
            source_commit=self.source_commit(),
            tool_paths=self.tools.paths() if self.tools else {},
            tool_sha256=self.tool_sha256() if self.tools else {},
            arch=self.platform.get("arch", "?"),
            kernel_release=self.platform.get("kernel_release", "?"),
            os_id=self.platform.get("os_id", "?"),
            os_version_id=self.platform.get("os_version_id", "?"),
            self_check=(f"PLT-009 substitute: `dmtcp_launch --version` = {version or 'none'} "
                        f"(pinned {ANALYSED_VERSION}); loopback confinement is re-verified at "
                        "every coordinator start (wildcard-bind refusal, port file, ss audit, "
                        "IPv4/IPv6 reachability). Kernel features are not checked."),
            requires_privileges=[], supports_leave_running=True, supports_cancel=False,
            supports_process_tree=False, supports_threads=True, restores_same_pid=False,
            verified=verified, verified_classes=list(VERIFIED_CLASSES) if verified else [],
            notes=notes)

    def _require_available(self) -> DmtcpTools:
        if self.tools is None:
            raise BackendError(ErrorCode.BACKEND_UNAVAILABLE, f"DMTCP not found: {BUILD_GUIDANCE}")
        if self.version != ANALYSED_VERSION:
            raise BackendError(ErrorCode.BACKEND_UNAVAILABLE,
                               f"DMTCP {self.version or 'unknown'} is not the analysed "
                               f"{ANALYSED_VERSION}",
                               detail={"reason": str(ReasonCode.UNANALYSED_VERSION)})
        return self.tools

    def assess(self, spec: WorkloadSpec) -> CompatibilityReport:
        """Static rules only (FR-005, CMP-006). Starts nothing."""
        findings = static_findings(spec.executable, self.platform.get("arch", "?"))
        if self.tools is not None and self.version != ANALYSED_VERSION:
            findings.append(Finding(ReasonCode.UNANALYSED_VERSION, str(self.version)))
        verified = self.tools is not None and self.platform_verified()
        if not verified:
            findings.append(Finding(ReasonCode.UNVERIFIED_PLATFORM, str(self.platform)))
        return decide(findings, reference_class=spec.reference_class, platform_verified=verified)

    # ------------------------------------------------------- coordinator start

    def _authorize(self) -> None:
        self.policy.authorize(os.getuid(), self._host_findings)

    def _guard(self) -> ListenerGuard:
        try:
            return ListenerGuard()
        except AuditError as exc:
            raise BackendError(ErrorCode.PERMISSION_DENIED,
                               f"cannot take a listener baseline, refusing to start: {exc}") from exc

    def _start_coordinator(self, tracker: ProcessTracker, ckpt_dir: Path, port_file: Path,
                           label: str, deadline: float) -> tuple[ManagedProcess, int,
                                                                  socket.socket, dict[str, Any]]:
        tools = self._require_available()
        self._authorize()  # nothing is bound or spawned before this
        listener = loopback_listener()
        port = int(listener.getsockname()[1])
        coord: ManagedProcess | None = None
        try:
            if not wildcard_bind_refused(port):
                raise BackendError(ErrorCode.PERMISSION_DENIED,
                                   f"kernel allowed 0.0.0.0:{port} next to the loopback listener; "
                                   "the fail-closed guarantee does not hold")
            try:
                coord = tracker.spawn(label, coordinator_argv(tools, port_file, ckpt_dir, port),
                                      cwd=ckpt_dir, fd_map=(listener.fileno(), PROTECTED_COORD_FD))
            except (OSError, RuntimeError) as exc:
                raise BackendError(ErrorCode.LAUNCH_FAILED, f"could not start {label}: {exc}") \
                    from exc
            popen = coord.popen
            assert popen is not None
            proc.wait_until(lambda: _read_port(port_file) is not None
                            or proc.leader_exited_unreaped(popen),
                            timeout=min(15.0, remaining(deadline, "coordinator start")))
            if _read_port(port_file) != port:
                raise BackendError(ErrorCode.PERMISSION_DENIED,
                                   f"{label} did not adopt the loopback listener on port {port}",
                                   detail={"stderr_tail": proc.log_tail(coord.stderr_path, 30)})
            facts = self._verify_isolation(coord, port, label)
            return coord, port, listener, facts
        except BaseException:
            if coord is not None:
                tracker.terminate(coord, sig=signal.SIGKILL)
            listener.close()
            raise

    @staticmethod
    def _verify_isolation(coord: ManagedProcess, port: int, label: str) -> dict[str, Any]:
        """Evidence that the coordinator is reachable only on loopback (fail-closed)."""
        try:
            listeners = current_listeners()
            addresses = non_loopback_ipv4_addresses() + non_loopback_ipv6_addresses()
        except AuditError as exc:
            raise BackendError(ErrorCode.PERMISSION_DENIED,
                               f"listener audit failed for {label}: {exc}") from exc
        on_port = [lst for lst in listeners if lst.port == port]
        exposed = listener_violations(listeners, {coord.ident.pid})
        reachable = {addr: tcp_reachable(addr, port) for addr in addresses}
        facts: dict[str, Any] = {"port": port, "listeners_on_port": [vars(x) for x in on_port],
                                 "coordinator_non_loopback": [vars(x) for x in exposed],
                                 "non_loopback_connect_results": reachable}
        if not on_port or not all(x.loopback for x in on_port) or exposed or any(reachable.values()):
            raise BackendError(ErrorCode.PERMISSION_DENIED,
                               f"{label} is reachable beyond loopback", detail=facts)
        return facts

    # ------------------------------------------------------------ sessions

    def _new_live(self, kind: str, session_dir: Path, env: dict[str, str],
                  deadline: float) -> _Live:
        make_private_dir(session_dir)
        ckpt_dir = make_private_dir(session_dir / "dmtcp-ckpt")
        tmp_dir = make_private_dir(session_dir / "dmtcp-tmp")
        self._require_available()
        self._authorize()
        guard = self._guard()  # baseline BEFORE anything is started
        tracker = ProcessTracker(session_dir / "logs", env=env or proc.filtered_env())
        coord, port, listener, facts = self._start_coordinator(
            tracker, ckpt_dir, session_dir / f"coordinator-{kind}.port", f"coordinator-{kind}",
            deadline)
        live = _Live(uuid.uuid4().hex, kind, tracker, guard, session_dir, ckpt_dir, tmp_dir,
                     coord, listener, port, notes={"isolation": facts})
        self._live[live.session_id] = live
        self._write_state(live)
        return live

    def _write_state(self, live: _Live) -> None:
        SessionState(str(live.session_dir), live.port, live.tracker.records()).write(
            live.session_dir / "session.json")

    def _get(self, session: BackendSession) -> _Live:
        live = self._live.get(session.session_id)
        if live is None:
            raise BackendError(ErrorCode.INVALID_ARGUMENT,
                               f"session {session.session_id} is not managed by this backend")
        return live

    def _session_pids(self, live: _Live) -> set[int]:
        pids: set[int] = set()
        for managed in live.tracker.processes:
            if managed.own_group and managed.popen is not None and managed.popen.returncode is None:
                pids.update(m.ident.pid for m in proc.running_group_members(managed.ident.pid))
            elif proc.alive(managed.ident):
                pids.add(managed.ident.pid)
        return pids

    def audit(self, session: BackendSession, step: str = "audit") -> None:
        self._audit(self._get(session), step)

    def _audit(self, live: _Live, step: str) -> None:
        """Kill the session and fail on any exposure, or if the audit cannot run."""
        try:
            pids = self._session_pids(live)
            exposed = live.guard.violations(pids, step)
        except AuditError as exc:
            self._abort(live, f"listener audit failed after {step}")
            raise BackendError(ErrorCode.PERMISSION_DENIED,
                               f"after {step}: listener audit failed, stopped: {exc}") from exc
        if exposed:
            self._abort(live, f"network exposure after {step}")
            raise BackendError(ErrorCode.PERMISSION_DENIED, f"network exposure after {step}",
                               detail=self._exposure_detail(live, step, exposed, pids))

    @staticmethod
    def _exposure_detail(live: _Live, step: str, exposed: Sequence[Any],
                         pids: set[int]) -> dict[str, Any]:
        """Bounded evidence: each listener's protocol, address, owner uid, inode, and
        visible pids, plus who this session's processes were when it was seen."""
        return {"step": step, "observed_utc": proc.utc_now(), "own_uid": os.getuid(),
                "listeners": [vars(x) for x in list(exposed)[:20]],
                "session_pids": sorted(pids)[:50],
                "session_processes": live.tracker.records()[:50],
                "host_sockets_seen": list(live.guard.notes)[:20]}

    def _abort(self, live: _Live, reason: str) -> None:
        live.tracker.event("abort", reason=reason)
        live.tracker.cleanup()
        live.listener.close()

    def _session(self, live: _Live, workload: ManagedProcess) -> BackendSession:
        return BackendSession(live.session_id, workload.ident.pid, workload.ident.start_ticks,
                              workload.ident.pid, [live.coordinator.ident.pid],
                              {"kind": live.kind, "port": live.port,
                               "session_dir": str(live.session_dir),
                               "checkpoint_dir": str(live.ckpt_dir)})

    # -------------------------------------------------------------- launch

    def launch(self, spec: WorkloadSpec, session_dir: Path, deadline: float) -> BackendSession:
        tools = self._require_available()
        report = self.assess(spec)
        if report.refused:
            raise BackendError(ErrorCode.UNSUPPORTED_WORKLOAD,
                               "workload refused: " + ", ".join(f.code for f in report.findings),
                               detail=report.to_json())
        if spec.argv[:1] != [str(spec.executable)]:
            raise BackendError(ErrorCode.INVALID_ARGUMENT, "argv[0] must be the executable path")
        remaining(deadline, "launch")
        live = self._new_live("launch", session_dir, spec.env, deadline)
        try:
            live.workload = live.tracker.spawn(
                "dmtcp-launch", launch_argv(tools, live.port, live.ckpt_dir, live.tmp_dir,
                                            spec.argv), cwd=spec.working_dir)
            self._write_state(live)
            self._audit(live, "launch")
        except BackendError:
            self._close(live)
            raise
        except (OSError, RuntimeError) as exc:
            self._close(live)
            raise BackendError(ErrorCode.LAUNCH_FAILED, f"dmtcp_launch failed: {exc}") from exc
        return self._session(live, live.workload)

    def status(self, session: BackendSession) -> SessionStatus:
        live = self._live.get(session.session_id)
        if live is None or live.workload is None:
            return SessionStatus(RunState.UNKNOWN, reason="not managed by this backend instance")
        wl = live.workload
        if wl.popen is not None:
            ended = proc.child_exit_status(wl.popen)
            if ended is not None:
                code = ended.get("exit_code")
                sig = ended.get("signal")
                return SessionStatus(RunState.EXITED,
                                     exit_code=code if isinstance(code, int) else None,
                                     signal=sig if isinstance(sig, int) else None)
            if wl.popen.returncode is not None:
                return SessionStatus(RunState.EXITED, exit_code=wl.popen.returncode)
        if proc.alive(wl.ident):
            return SessionStatus(RunState.ALIVE)
        return SessionStatus(RunState.UNKNOWN, reason="identity no longer matches")

    # ----------------------------------------------------------- checkpoint

    def checkpoint(self, session: BackendSession, staging_dir: Path, options: CheckpointOptions,
                   deadline: float) -> CaptureResult:
        """Verified checkpoint into `staging_dir`. The workload is left running on failure."""
        live = self._get(session)
        tools = self._require_available()
        if not options.leave_running:
            raise BackendError(ErrorCode.INVALID_ARGUMENT, "Stage 03a supports leave_running only")
        wl = live.workload
        if wl is None or not proc.alive(wl.ident):
            raise BackendError(ErrorCode.CHECKPOINT_FAILED, "the workload is not running")
        findings = runtime_findings(collect_runtime_facts(wl.ident.pid, wl.ident.pid),
                                    PROTECTED_FDS)
        if findings:
            raise BackendError(ErrorCode.UNSUPPORTED_WORKLOAD,
                               "workload refused before checkpoint (left running): "
                               + ", ".join(f.code for f in findings),
                               detail={"findings": [f.to_json() for f in findings]})
        # Stage 02 verified restores only from the directory DMTCP wrote the images to, so
        # Stage 03a never moves images: the staging directory IS the session's checkpoint
        # directory (session.data["checkpoint_dir"]). Publication is Stage 04's job.
        if staging_dir.resolve() != live.ckpt_dir.resolve():
            raise BackendError(ErrorCode.INVALID_ARGUMENT,
                               f"Stage 03a checkpoints stay where DMTCP writes them: staging_dir "
                               f"must be {live.ckpt_dir}")
        self._audit(live, "before checkpoint")
        since_ns = time.time_ns() - 1_000_000_000  # 1 s slack for filesystem timestamps
        started = time.monotonic()
        request = live.tracker.run("dmtcp-checkpoint",
                                   command_argv(tools, live.port, "--checkpoint"),
                                   timeout=min(30.0, remaining(deadline, "checkpoint request")))
        if not request.ok:
            self._discard_partial(live)
            raise BackendError(ErrorCode.CHECKPOINT_FAILED,
                               f"checkpoint request failed: {request.summary()}", retryable=True)
        coord = live.coordinator

        def liveness() -> str | None:
            if not proc.alive(wl.ident):
                return "workload died while the checkpoint was in progress"
            if coord.popen is not None and proc.leader_exited_unreaped(coord.popen):
                return "coordinator exited while the checkpoint was in progress"
            return None

        waited = wait_for_checkpoint(
            live.ckpt_dir, live.port, since_ns, live.expected_images,
            timeout=min(CHECKPOINT_WAIT_CAP_S, remaining(deadline, "checkpoint wait")),
            liveness=liveness, audit=lambda: self._audit(live, "checkpoint (waiting)"))
        status = live.tracker.run("dmtcp-status", command_argv(tools, live.port, "--status"),
                                  timeout=min(15.0, max(1.0, deadline - time.monotonic())))
        fields = parse_dmtcp_status(status.stdout())
        detail: dict[str, Any] = {
            "wait_reason": waited.reason, "wait_polls": waited.polls,
            "images": [vars(i) for i in waited.evidence.images],
            "restart_scripts": waited.evidence.scripts, "temp_files": waited.evidence.temp_files,
            "status_after": fields, "coordinator_log_tail": proc.log_tail(coord.stderr_path, 30)}
        if not waited.ok:
            self._discard_partial(live)
            timed_out = waited.reason.startswith("timed out")
            raise BackendError(ErrorCode.TIMED_OUT if timed_out else ErrorCode.CHECKPOINT_FAILED,
                               f"checkpoint not verified complete: {waited.reason}",
                               retryable=timed_out, detail=detail)
        if fields.get("NUM_PEERS") != str(live.expected_images) or fields.get("RUNNING") != "yes":
            self._discard_partial(live)
            raise BackendError(ErrorCode.CHECKPOINT_FAILED,
                               f"coordinator status after checkpoint is {fields}", detail=detail)
        images = [i.name for i in waited.evidence.images]
        scripts = list(waited.evidence.scripts)
        artifacts.fsync_dir(live.ckpt_dir)  # images are renamed into place: make it durable
        entries = artifacts.inspect_all(
            live.ckpt_dir, [(n, ArtifactRole.IMAGE) for n in images]
            + [(n, ArtifactRole.RESTART_SCRIPT) for n in scripts])
        self._audit(live, "after checkpoint")
        excerpt = "\n".join(proc.log_tail(coord.stderr_path, 40))[-4096:]
        return CaptureResult(entries, {"images": images, "restart_scripts": scripts,
                                       "expected_processes": live.expected_images,
                                       "filesystem": artifacts.filesystem_type(live.ckpt_dir),
                                       "port_at_capture": live.port},
                             int((time.monotonic() - started) * 1000), excerpt)

    def capture_record(self, capture: CaptureResult) -> CaptureRecord:
        """Manifest facts the restore must match (CMP-004, CMP-005)."""
        return CaptureRecord(self.name, self.version or "unknown", self.tool_sha256(),
                             self.platform.get("arch", "?"),
                             self.platform.get("kernel_release", "?"),
                             list(capture.artifacts), dict(capture.restore_hints))

    @staticmethod
    def _discard_partial(live: _Live) -> None:
        """Remove TEMPORARY engine output of a failed checkpoint (session directory only).

        Final-name images are whole images (renamed into place by DMTCP) and may belong to
        an earlier good capture, so they are never deleted here (REL-004).
        """
        try:
            with artifacts.private_dir(live.ckpt_dir) as dfd:
                for name in os.listdir(dfd):
                    if name.endswith((".temp", ".tmp")):
                        try:
                            os.unlink(name, dir_fd=dfd)
                        except OSError:
                            pass
        except BackendError:
            pass

    def stop(self, session: BackendSession) -> bool:
        """Kill the workload and stop the session's coordinator (used before a restore)."""
        live = self._get(session)
        tools = self._require_available()
        gone = live.workload is None or live.tracker.terminate(live.workload, sig=signal.SIGKILL)
        live.tracker.run("dmtcp-quit", command_argv(tools, live.port, "--quit"), timeout=15)
        coord_gone = live.tracker.terminate(live.coordinator)
        live.listener.close()
        return gone and coord_gone

    # -------------------------------------------------------------- restore

    def restore(self, capture: CaptureRecord, artifact_dir: Path, options: RestoreOptions,
                deadline: float) -> BackendSession:
        """Start the restore. Success is decided by the caller's verification (FR-026)."""
        tools = self._require_available()
        if capture.backend != self.name or capture.backend_version != self.version:
            raise BackendError(ErrorCode.VERSION_MISMATCH,
                               f"capture is {capture.backend} {capture.backend_version}, engine is "
                               f"{self.name} {self.version}")
        if capture.tool_sha256 != self.tool_sha256():
            raise BackendError(ErrorCode.VERSION_MISMATCH, "DMTCP tool binaries changed since capture")
        if capture.arch != self.platform.get("arch"):
            raise BackendError(ErrorCode.ARCH_MISMATCH,
                               f"capture is {capture.arch}, host is {self.platform.get('arch')}")
        entries = artifacts.verify(artifact_dir, capture.artifacts)
        images = [artifact_dir / e.relative_path for e in entries if e.role is ArtifactRole.IMAGE]
        if len(images) != int(capture.restore_hints.get("expected_processes", 1)):
            raise BackendError(ErrorCode.INVALID_ARTIFACTS,
                               f"{len(images)} image(s) for "
                               f"{capture.restore_hints.get('expected_processes')} process(es)")
        remaining(deadline, "restore")
        live = self._new_live("restore", options.session_dir, {}, deadline)
        live.notes["kernel_release_at_capture"] = capture.kernel_release
        try:
            live.notes["not_before_ticks"] = proc.boot_ticks_now()
            live.workload = live.tracker.spawn("dmtcp-restart",
                                               restart_argv(tools, live.port, live.tmp_dir, images),
                                               cwd=options.working_dir)
            self._write_state(live)
            popen = live.workload.popen
            assert popen is not None
            proc.wait_until(lambda: proc.leader_exited_unreaped(popen),
                            timeout=min(RESTORE_FAIL_FAST_S, remaining(deadline, "restore start")))
            if proc.leader_exited_unreaped(popen):
                detail = self.restore_diagnostics_live(live)
                raise BackendError(ErrorCode.RESTORE_FAILED,
                                   f"dmtcp_restart exited within {RESTORE_FAIL_FAST_S:.0f} s",
                                   detail=detail)
            self._audit(live, "restore")
        except BackendError:
            self._close(live)
            raise
        except (OSError, RuntimeError) as exc:
            self._close(live)
            raise BackendError(ErrorCode.RESTORE_FAILED, f"dmtcp_restart failed: {exc}") from exc
        session = self._session(live, live.workload)
        session.data["not_before_ticks"] = live.notes["not_before_ticks"]
        return session

    def adopt_restored(self, session: BackendSession, pid: int) -> bool:
        """Track a restored process (by kernel PID) only if it is ours (SEC-003, LIM-006):
        this user, a session this restore created, started after the restore began."""
        live = self._get(session)
        not_before = live.notes.get("not_before_ticks")
        adopted = live.tracker.adopt("restored", pid,
                                     not_before if isinstance(not_before, int) else None)
        if adopted is not None:
            self._write_state(live)
        return adopted is not None

    @staticmethod
    def leak_scan(path: Path) -> list[str]:
        """Live processes of this user that still reference `path`. Reported, never killed."""
        return [f"pid {p.ident.pid}" for p in proc.leaked_processes(path)]

    def network_notes(self, session: BackendSession) -> list[dict[str, Any]]:
        """Host sockets of system accounts seen (and not counted) during this session."""
        live = self._live.get(session.session_id)
        return list(live.guard.notes) if live is not None else []

    def restore_diagnostics(self, session: BackendSession) -> dict[str, Any]:
        live = self._live.get(session.session_id)
        return self.restore_diagnostics_live(live) if live is not None else {}

    @staticmethod
    def restore_diagnostics_live(live: _Live) -> dict[str, Any]:
        """Exit status (read without reaping) and unfiltered logs of the restarter."""
        wl = live.workload
        return {
            "restarter_exit": proc.child_exit_status(wl.popen)
            if wl is not None and wl.popen is not None else None,
            "restarter_stderr_tail": proc.log_tail(wl.stderr_path if wl else None, 60),
            "restarter_stdout_tail": proc.log_tail(wl.stdout_path if wl else None, 20),
            "coordinator_log_tail": proc.log_tail(live.coordinator.stderr_path, 30),
            "host_sockets_seen": list(live.guard.notes)[:20],
        }

    # ------------------------------------------------------- cancel/cleanup

    def cancel(self, session: BackendSession) -> CancelOutcome:
        return CancelOutcome.NOT_SUPPORTED

    def _close(self, live: _Live) -> list[str]:
        problems = []
        if self.tools is not None and live.coordinator.popen is not None \
                and live.coordinator.popen.returncode is None:
            live.tracker.run("dmtcp-quit", command_argv(self.tools, live.port, "--quit"),
                             timeout=15)
        problems += [f"survived: {name}" for name in live.tracker.cleanup()]
        live.listener.close()
        self._live.pop(live.session_id, None)
        own = {m.ident.pid for m in live.tracker.processes}
        problems += [f"leaked: pid {p.ident.pid}"
                     for p in proc.leaked_processes(live.session_dir, exclude=sorted(own))
                     if proc.read_info(p.ident.pid) is not None]
        self._write_state(live)
        return problems

    def cleanup(self, session: BackendSession | None, path: Path | None) -> list[str]:
        """Stop the session's processes and report anything left. Deletes nothing: the
        session directory is evidence; storage (Stage 04) owns artifact lifetime."""
        if session is None:
            return []
        live = self._live.get(session.session_id)
        return self._close(live) if live is not None else []

    def close_all(self) -> list[str]:
        problems: list[str] = []
        for live in list(self._live.values()):
            problems += self._close(live)
        return problems


def _read_port(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None
