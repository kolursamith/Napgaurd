"""DMTCP and CRIU experiment drivers (Stage 02 only; not the Stage 03 adapter).

Each driver exposes the same steps the trial needs: probe, launch, checkpoint,
stop the original, restore, and identify the restored processes. Argument
vectors are built by pure functions so they can be unit-tested on any host.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ckptwait import parse_dmtcp_status, wait_for_checkpoint
from .results import Check, Status
from .runner import (
    ArtifactSet,
    Recorder,
    Tracked,
    alive,
    child_exit_status,
    collect_artifacts,
    current_listeners,
    leader_exited_unreaped,
    listener_violations,
    loopback_listener,
    non_loopback_ipv4_addresses,
    non_loopback_ipv6_addresses,
    read_text,
    socket_fds,
    tcp_reachable,
    wait_until,
)

DEFAULT_PREFIX = Path.home() / ".local" / "opt" / "napguard-stage02"
LOOPBACK = "127.0.0.1"

# OD-13 / security finding I-1. Verified in the DMTCP v4.2.0 source (and on `main`):
# dmtcp_coordinator.cpp:1745 and coordinatorapi.cpp:490 bind the coordinator's
# listener to JSockAddr::ANY (INADDR_ANY), there is no option to choose the bind
# address, `--no-coordinator` no longer exists, and the coordinator protocol has no
# authentication. A coordinator would therefore be reachable from the VM's network.
# Until a supported loopback-only mechanism is verified and approved, the harness
# refuses to start one. Do not flip this flag without an approved decision.
DMTCP_LOOPBACK_ONLY_COORDINATOR_VERIFIED = False
DMTCP_COORDINATOR_BLOCKER = (
    "refusing to start a DMTCP coordinator: DMTCP 4.2.0 binds it to all network interfaces "
    "(JSockAddr::ANY) with no authentication and no supported loopback-only option "
    "(security finding I-1, open decision OD-13)"
)


# Candidate OD-13 mitigation (Stage 02 investigation): hand the coordinator a listener
# that the harness has already bound to 127.0.0.1. In DMTCP 4.2.0 the coordinator first
# checks for an inherited listener on PROTECTED_COORD_FD and uses it
# (dmtcp_coordinator.cpp:1738-1742); dmtcp_launch --new-coordinator uses the same hand-off
# internally (coordinatorapi.cpp:490-508), so this is DMTCP's own mechanism, but it is not
# a documented user option. PROTECTED_COORD_FD = PROTECTED_FD_START (820) + 1
# (include/protectedfds.h). The analysis is valid only for the pinned version.
# Fail-closed: the coordinator is also told `--coord-port <same port>`. If it ignored the
# inherited listener and fell back to binding INADDR_ANY:<port>, Linux refuses that bind
# with EADDRINUSE while our 127.0.0.1:<port> socket is listening, and the coordinator exits.
PROTECTED_COORD_FD = 821
FD_HANDOFF_ANALYSED_VERSION = "4.2.0"
# DMTCP keeps its own descriptors (coordinator connection, restore sockets, ...) at
# PROTECTED_FD_START = 820 and upward (include/protectedfds.h lists ~25 entries). Sockets in
# this range belong to DMTCP, not to the workload. Anything below it is a workload socket.
DMTCP_PROTECTED_FDS = range(820, 860)

CHECKPOINT_TIMEOUT_S = 180.0  # bounded wait for a verified-complete checkpoint


def coordinator_log_notes(coord: Tracked) -> list[str]:
    """Checkpoint-related lines from the coordinator's own log (diagnostic evidence)."""
    if coord.stderr_path is None or not coord.stderr_path.exists():
        return []
    keys = ("Checkpoint complete", "checkpoint", "unhandled user command", "error", "Error")
    lines = coord.stderr_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return [line.strip() for line in lines if any(k in line for k in keys)][-20:]


def log_tail(path: Path | None, lines: int = 60) -> list[str]:
    """The last `lines` lines of a captured log, UNFILTERED (diagnostic evidence).

    Keyword filtering (coordinator_log_notes) can hide the fatal line and surface an
    incidental one, e.g. a JASSERT_ERRNO warning matches "error" via "strerror".
    """
    if path is None or not path.exists():
        return []
    return [line.rstrip() for line in
            path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]]


def restart_failure_facts(restarter: Tracked, coord: Tracked) -> dict[str, object]:
    """Read-only evidence for a dmtcp_restart that exited early. Reaps nothing."""
    return {
        "log": coordinator_log_notes(restarter),
        "restarter_exit": (child_exit_status(restarter.popen)
                           if restarter.popen is not None else None),
        "restarter_stderr_tail": log_tail(restarter.stderr_path),
        "restarter_stdout_tail": log_tail(restarter.stdout_path, 20),
        "restarter_stderr_path": str(restarter.stderr_path) if restarter.stderr_path else None,
        "restore_coordinator_tail": log_tail(coord.stderr_path, 30),
    }


# ADR 0004 (accepted, limited): c-single, same boot and (Amendment 1) cross-reboot.
CONTROLLED_TRIAL_CLASSES = ("c-single",)

# ADR 0004 Amendment 2: APPROVED by the project owner on 2026-10-20 for exactly one
# same-boot `trial` per class on the single-user Ubuntu 24.04 ARM64 UTM VM. Not production.
# Same-boot `trial` only: never `matrix`, never reboot-prepare/reboot-resume.
AMENDMENT_2_CLASSES = ("c-threads", "py", "c-file-static", "c-file-grow")
AMENDMENT_2_APPROVED = True
# Never runnable under ADR 0004, whatever is approved: multi-process, sockets (I-1c), GUI.
NEVER_UNDER_ADR_0004 = ("c-child", "tcp", "gui")
# ADR 0004 Amendment 3: a separate, ONE-TIME owner approval (2026-10-20) for exactly one
# additional same-boot `py` DIAGNOSTIC retest after the first py restore failed (52e5bae).
# Value = launched controlled runs of the class that must ALREADY exist in the run root:
# the retest needs the preserved original failed run there, and is refused once any
# further py run has launched a workload.
ONE_TIME_RETESTS = {"py": 1}


def workload_socket_fds(pid: int) -> list[tuple[int, str]]:
    """Sockets the workload itself holds (DMTCP's protected descriptors excluded).

    DMTCP opens a restore-time listener on INADDR_ANY only when the image contains
    workload sockets (plugin/ipc/socket/connectionrewirer.cpp:173-207), so a controlled
    trial must checkpoint a process for which this list is empty.
    """
    return [(fd, link) for fd, link in socket_fds(pid) if fd not in DMTCP_PROTECTED_FDS]


def wildcard_bind_refused(port: int) -> bool:
    """True if binding 0.0.0.0:port fails with EADDRINUSE (the fail-closed property)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("0.0.0.0", port))
    except OSError as exc:
        return exc.errno == errno.EADDRINUSE
    finally:
        probe.close()
    return False


@dataclass
class Session:
    """Everything a trial needs to know about one backend-managed computation."""

    workload: Tracked
    helpers: list[Tracked] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------- DMTCP


@dataclass(frozen=True)
class DmtcpTools:
    launch: str
    command: str
    coordinator: str
    restart: str


def find_dmtcp(prefix: Path | None) -> DmtcpTools | None:
    names = ("dmtcp_launch", "dmtcp_command", "dmtcp_coordinator", "dmtcp_restart")
    candidates: list[Path | None] = []
    if prefix is not None:
        candidates.append(prefix / "bin")
    candidates.extend(sorted(DEFAULT_PREFIX.glob("dmtcp-*/bin"), reverse=True))
    candidates.append(None)  # PATH
    for base in candidates:
        found = [
            str(base / n) if base is not None and (base / n).is_file() else
            (shutil.which(n) if base is None else None)
            for n in names
        ]
        if all(found):
            return DmtcpTools(*(str(Path(f).resolve()) for f in found if f))
    return None


def dmtcp_coordinator_argv(tools: DmtcpTools, port_file: Path, ckpt_dir: Path,
                           port: int) -> list[str]:
    # `port` is the port of the pre-bound loopback listener handed over on fd 821; passing
    # the same port makes any fallback INADDR_ANY bind fail (see FD_HANDOFF_ANALYSED_VERSION).
    return [tools.coordinator, "--coord-port", str(port), "--port-file", str(port_file),
            "--ckptdir", str(ckpt_dir)]


def dmtcp_launch_argv(
    tools: DmtcpTools, port: int, ckpt_dir: Path, tmp_dir: Path, workload: Sequence[str]
) -> list[str]:
    return [tools.launch, "--coord-host", LOOPBACK, "--coord-port", str(port),
            "--join-coordinator", "--ckptdir", str(ckpt_dir), "--tmpdir", str(tmp_dir),
            *workload]


def dmtcp_command_argv(tools: DmtcpTools, port: int, *command: str) -> list[str]:
    return [tools.command, "--coord-host", LOOPBACK, "--coord-port", str(port), *command]


def dmtcp_restart_argv(
    tools: DmtcpTools, port: int, tmp_dir: Path, images: Sequence[Path]
) -> list[str]:
    return [tools.restart, "--coord-host", LOOPBACK, "--coord-port", str(port),
            "--join-coordinator", "--tmpdir", str(tmp_dir), *map(str, images)]


class Dmtcp:
    name = "dmtcp"

    def __init__(self, rec: Recorder, prefix: Path | None, controlled_trial: bool = False) -> None:
        self.rec = rec
        self.tools = find_dmtcp(prefix)
        # ADR 0004 (proposed) per-invocation exception; the global gate stays closed.
        self.controlled_trial = controlled_trial
        self.ckpt_dir = rec.run_dir / "checkpoint"
        self.tmp_dir = rec.run_dir / "dmtcp-tmp"
        self.version = "unknown"
        self.listeners: list[socket.socket] = []  # held open until cleanup

    def probe(self) -> Check:
        if self.tools is None:
            return Check("backend available", Status.BLOCKED,
                         "DMTCP tools not found (build with experiments/stage02/build-dmtcp.sh)")
        res = self.rec.run("dmtcp-version", [self.tools.launch, "--version"], timeout=15)
        text = (res.stdout() + res.stderr()).strip()
        self.version = text.splitlines()[0] if text else "unknown"
        facts = {"tools": self.tools.__dict__, "version": self.version}
        if not res.ok:
            return Check("backend available", Status.BLOCKED,
                         f"dmtcp_launch --version failed: {res.summary()}", facts=facts)
        if self.controlled_trial and FD_HANDOFF_ANALYSED_VERSION not in self.version:
            return Check("backend available", Status.BLOCKED,
                         f"ADR 0004 covers DMTCP {FD_HANDOFF_ANALYSED_VERSION} only; found "
                         f"{self.version!r}", facts=facts)
        if self.controlled_trial:
            return Check("backend available", Status.PASS,
                         f"DMTCP {self.version} under the ADR 0004 controlled-trial exception "
                         "(loopback fd hand-off, c-single only, listener audits enforced)",
                         facts=facts)
        if not DMTCP_LOOPBACK_ONLY_COORDINATOR_VERIFIED:
            return Check("backend available", Status.BLOCKED,
                         f"DMTCP {self.version} installed, but {DMTCP_COORDINATOR_BLOCKER}",
                         facts=facts)
        return Check("backend available", Status.PASS, f"DMTCP {self.version}", facts=facts)

    def _coordinator(self, label: str) -> tuple[Tracked, int]:
        """Coordinator for trials: refused unless the global gate or the ADR 0004 exception allows it.

        Either way, the coordinator only ever starts through the fail-closed loopback hand-off.
        """
        if not (DMTCP_LOOPBACK_ONLY_COORDINATOR_VERIFIED or self.controlled_trial):
            raise BackendStepError(DMTCP_COORDINATOR_BLOCKER)  # nothing has been spawned
        coord, port, _ = self._loopback_coordinator(label)
        return coord, port

    def _loopback_coordinator(self, label: str) -> tuple[Tracked, int, dict[str, Any]]:
        """Start a coordinator on a pre-bound 127.0.0.1 listener; abort on any exposure."""
        assert self.tools is not None
        if FD_HANDOFF_ANALYSED_VERSION not in self.version:
            raise BackendStepError(f"fd hand-off was analysed for DMTCP {FD_HANDOFF_ANALYSED_VERSION} "
                                   f"only; found {self.version!r}")
        listener = loopback_listener()
        port = int(listener.getsockname()[1])
        if not wildcard_bind_refused(port):
            listener.close()
            raise BackendStepError(f"kernel allowed 0.0.0.0:{port} next to the loopback listener; "
                                   "the fail-closed guarantee does not hold, refusing")
        self.listeners.append(listener)
        port_file = self.rec.run_dir / f"{label}.port"
        self.ckpt_dir.mkdir(exist_ok=True, mode=0o700)
        try:
            coord = self.rec.spawn(label, dmtcp_coordinator_argv(self.tools, port_file,
                                                                 self.ckpt_dir, port),
                                   cwd=self.ckpt_dir,
                                   fd_map=(listener.fileno(), PROTECTED_COORD_FD))
        except (OSError, RuntimeError) as exc:
            raise BackendStepError(f"could not start {label}: {exc}") from exc
        assert coord.popen is not None
        popen = coord.popen
        wait_until(lambda: read_text(str(port_file), "").isdigit()
                   or leader_exited_unreaped(popen), timeout=15)
        written = read_text(str(port_file), "")
        if not written.isdigit() or int(written) != port:
            self.rec.terminate(coord, sig=signal.SIGKILL)
            raise BackendStepError(f"{label} did not adopt the loopback listener on port {port}")
        facts = self.verify_isolation(coord, port, label)
        return coord, port, facts

    def verify_isolation(self, coord: Tracked, port: int, label: str) -> dict[str, Any]:
        """Evidence that the coordinator is reachable only on loopback; kill it otherwise.

        Fails closed: if the audit itself cannot run (ss/ip missing, error, timeout), the
        coordinator is killed and the step fails.
        """
        try:
            listeners = current_listeners()
            addresses = non_loopback_ipv4_addresses() + non_loopback_ipv6_addresses()
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            self.rec.terminate(coord, sig=signal.SIGKILL)
            raise BackendStepError(f"listener audit failed for {label}: {exc}") from exc
        on_port = [lst for lst in listeners if lst.port == port]
        exposed = listener_violations(listeners, {coord.ident.pid})
        reachable = {addr: tcp_reachable(addr, port) for addr in addresses}
        facts: dict[str, Any] = {
            "port": port,
            "listeners_on_port": [lst.__dict__ for lst in on_port],
            "coordinator_non_loopback_listeners": [lst.__dict__ for lst in exposed],
            "non_loopback_connect_results": reachable,
        }
        self.rec.event("dmtcp_isolation", label=label, **facts)
        if not on_port or not all(lst.loopback for lst in on_port) or exposed or any(
                reachable.values()):
            self.rec.terminate(coord, sig=signal.SIGKILL)
            raise BackendStepError(f"{label} is reachable beyond loopback: {facts}")
        return facts

    def isolation_check(self) -> list[Check]:
        """Start one loopback coordinator, prove isolation and function, then stop it.

        Runs no workload and takes no checkpoint. Does not open the trial gate.
        """
        checks = [self.probe()]
        if self.tools is None or "dmtcp_launch --version failed" in checks[0].detail:
            return checks
        checks[0] = Check("backend available", Status.PASS, f"DMTCP {self.version}",
                          facts=checks[0].facts)
        try:
            coord, port, facts = self._loopback_coordinator("isolation-coordinator")
        except BackendStepError as exc:
            checks.append(Check("coordinator confined to loopback", Status.FAIL, str(exc)))
            return checks
        checks.append(Check(
            "coordinator confined to loopback", Status.PASS,
            f"listens only on 127.0.0.1:{port}; connections to "
            f"{sorted(facts['non_loopback_connect_results']) or 'no other IPv4 address'} refused",
            facts=facts))
        status = self.rec.run("dmtcp-status", dmtcp_command_argv(self.tools, port, "--status"),
                              timeout=15)
        checks.append(Check("coordinator answers on loopback",
                            Status.PASS if status.ok else Status.FAIL, status.summary()))
        self.rec.run("dmtcp-quit", dmtcp_command_argv(self.tools, port, "--quit"), timeout=15)
        gone = self.rec.terminate(coord)
        checks.append(Check("coordinator stopped", Status.PASS if gone else Status.FAIL,
                            "coordinator process gone" if gone else "coordinator still alive"))
        return checks

    def launch(self, workload: Sequence[str]) -> Session:
        assert self.tools is not None
        self.tmp_dir.mkdir(exist_ok=True, mode=0o700)
        coord, port = self._coordinator("coordinator")
        wl = self.rec.spawn(
            "dmtcp-launch", dmtcp_launch_argv(self.tools, port, self.ckpt_dir, self.tmp_dir, workload)
        )
        return Session(wl, [coord], {"port": port})

    def checkpoint(self, session: Session,
                   audit: Callable[[str], None] = lambda step: None) -> tuple[Check, ArtifactSet]:
        """Request a checkpoint, then wait for verified completion (never trust exit 0).

        `--bcheckpoint` does not block in DMTCP 4.2.0 (see ngexp/ckptwait.py), so the plain
        `--checkpoint` request is followed by a bounded wait for protocol and artifact
        evidence, with liveness checks and listener audits on every poll.
        """
        assert self.tools is not None
        port = int(session.data["port"])
        coord = session.helpers[0]
        # Stage 02 DMTCP trials are single-process (ADR 0004: c-single), hence one image.
        expected = 1
        self.rec.run("dmtcp-list-before", dmtcp_command_argv(self.tools, port, "--list"), timeout=15)
        since_ns = time.time_ns() - 1_000_000_000  # 1 s slack for filesystem timestamps
        started = time.monotonic()
        res = self.rec.run("dmtcp-checkpoint", dmtcp_command_argv(self.tools, port, "--checkpoint"),
                           timeout=30)
        facts: dict[str, Any] = {"request": res.summary(), "expected_images": expected}
        if not res.ok:
            return (Check("native checkpoint created", Status.FAIL,
                          f"checkpoint request failed: {res.summary()}", facts=facts),
                    collect_artifacts(self.ckpt_dir, self.rec.run_dir, ["*"]))

        def liveness() -> str | None:
            if not alive(session.workload.ident):
                return "workload died while the checkpoint was in progress"
            if coord.popen is not None and leader_exited_unreaped(coord.popen):
                return "coordinator exited while the checkpoint was in progress"
            return None

        waited = wait_for_checkpoint(self.ckpt_dir, port, since_ns, expected,
                                     timeout=CHECKPOINT_TIMEOUT_S, liveness=liveness,
                                     audit=lambda: audit("checkpoint (waiting)"))
        status = self.rec.run("dmtcp-status-after", dmtcp_command_argv(self.tools, port,
                                                                        "--status"), timeout=15)
        status_fields = parse_dmtcp_status(status.stdout())
        coord_log = coordinator_log_notes(coord)
        artifacts = collect_artifacts(self.ckpt_dir, self.rec.run_dir,
                                      ["ckpt_*.dmtcp", "dmtcp_restart_script*.sh", "*.temp"])
        facts.update({
            "duration_s": round(time.monotonic() - started, 3),
            "wait_polls": waited.polls,
            "wait_reason": waited.reason,
            "images": [img.__dict__ for img in waited.evidence.images],
            "restart_scripts": waited.evidence.scripts,
            "temp_files": waited.evidence.temp_files,
            "stale_files": waited.evidence.stale,
            "status_after": status_fields,
            "coordinator_log": coord_log,
            "checkpoint_dir_listing": sorted(p.name for p in self.ckpt_dir.iterdir())
            if self.ckpt_dir.is_dir() else [],
        })
        if not waited.ok:
            return Check("native checkpoint created", Status.FAIL,
                         f"checkpoint not verified complete: {waited.reason}", facts=facts), artifacts
        if status_fields.get("NUM_PEERS") != str(expected) or status_fields.get("RUNNING") != "yes":
            return Check("native checkpoint created", Status.FAIL,
                         f"coordinator status after checkpoint is {status_fields} (expected "
                         f"NUM_PEERS={expected}, RUNNING=yes)", facts=facts), artifacts
        total = sum(img.size for img in waited.evidence.images)
        return Check("native checkpoint created", Status.PASS,
                     f"{len(waited.evidence.images)} DMTCP image(s), {total} bytes; restart script "
                     f"{waited.evidence.scripts}; stable; workers RUNNING; "
                     f"{facts['duration_s']:.2f}s", facts=facts), artifacts

    def stop_original(self, session: Session) -> bool:
        assert self.tools is not None
        gone = self.rec.terminate(session.workload, sig=signal.SIGKILL)
        self.rec.run("dmtcp-quit-original", dmtcp_command_argv(self.tools, int(session.data["port"]),
                                                                "--quit"), timeout=15)
        helpers_gone = all(self.rec.terminate(h) for h in session.helpers)
        return gone and helpers_gone

    def restore(self, images: Sequence[Path], not_before_ticks: int) -> tuple[Check, Session | None]:
        assert self.tools is not None
        coord, port = self._coordinator("coordinator-restore")
        restarter = self.rec.spawn("dmtcp-restart",
                                   dmtcp_restart_argv(self.tools, port, self.tmp_dir, images))
        time.sleep(1.0)  # give dmtcp_restart a moment to fail fast (without reaping it)
        exited = restarter.popen is not None and leader_exited_unreaped(restarter.popen)
        session = Session(restarter, [coord], {"port": port})
        if exited:
            return (Check("restore command started", Status.FAIL,
                          "dmtcp_restart exited within 1 s",
                          facts=restart_failure_facts(restarter, coord)), session)
        return (Check("restore command started", Status.PASS,
                      "dmtcp_restart running; restoration NOT yet verified (see state continuity)"),
                session)

    def restored_pid(self, kpid: int | None, pid: int) -> int | None:
        return kpid  # DMTCP virtualizes getpid(); only the kernel PID identifies the process

    def cleanup(self, sessions: Sequence[Session]) -> None:
        if self.tools is not None:
            for session in sessions:
                if "port" in session.data:
                    self.rec.run("dmtcp-quit", dmtcp_command_argv(
                        self.tools, int(session.data["port"]), "--quit"), timeout=15)
        for listener in self.listeners:
            listener.close()
        self.listeners.clear()

    def image_paths(self, artifacts: ArtifactSet) -> list[Path]:
        return [self.rec.run_dir / a.path for a in artifacts.items
                if a.path.endswith(".dmtcp") and not a.is_symlink]


# ---------------------------------------------------------------------- CRIU


def find_criu(explicit: Path | None) -> str | None:
    candidates = [explicit] if explicit else []
    candidates += sorted(DEFAULT_PREFIX.glob("criu-*/sbin/criu"), reverse=True)
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return str(candidate.resolve())
    found = shutil.which("criu")
    return str(Path(found).resolve()) if found else None


def criu_prefix(mode: str) -> list[str]:
    if mode == "sudo":
        return ["sudo", "-n"]
    if mode == "unprivileged":
        return []
    raise ValueError(f"unknown CRIU mode {mode!r}")


def criu_global(mode: str) -> list[str]:
    return ["--unprivileged"] if mode == "unprivileged" else []


def criu_dump_argv(criu: str, mode: str, pid: int, images: Path, log: Path) -> list[str]:
    return [*criu_prefix(mode), criu, "dump", *criu_global(mode), "--tree", str(pid),
            "--images-dir", str(images), "--log-file", str(log), "-v4", "--leave-running"]


def criu_restore_argv(criu: str, mode: str, images: Path, log: Path, pidfile: Path) -> list[str]:
    return [*criu_prefix(mode), criu, "restore", *criu_global(mode), "--images-dir", str(images),
            "--log-file", str(log), "-v4", "--restore-detached", "--pidfile", str(pidfile)]


def parse_getcap(output: str) -> set[str]:
    """Capability names from `getcap FILE` output, e.g. 'f cap_a,cap_b=ep'."""
    match = re.search(r"\s((?:cap_[a-z_]+,?)+)[=+]", output)
    return set(match.group(1).split(",")) if match else set()


class Criu:
    name = "criu"

    def __init__(self, rec: Recorder, criu_bin: Path | None, mode: str) -> None:
        self.rec = rec
        self.mode = mode
        self.criu = find_criu(criu_bin)
        self.images = rec.run_dir / "checkpoint"
        self.version = "unknown"

    def probe(self) -> Check:
        if self.criu is None:
            return Check("backend available", Status.BLOCKED,
                         "criu not found (build with experiments/stage02/build-criu.sh)")
        res = self.rec.run("criu-version", [self.criu, "--version"], timeout=15)
        self.version = " ".join(res.stdout().split()) or "unknown"
        if not res.ok:
            return Check("backend available", Status.BLOCKED, f"criu --version: {res.summary()}")
        facts: dict[str, Any] = {"criu": self.criu, "version": self.version, "mode": self.mode}
        if self.mode == "sudo":
            sudo = self.rec.run("sudo-check", ["sudo", "-n", "true"], timeout=15)
            if not sudo.ok:
                return Check("backend available", Status.BLOCKED,
                             "sudo mode requested but `sudo -n true` failed (run `sudo -v` first)",
                             facts=facts)
        else:
            caps: set[str] = set()
            if shutil.which("getcap"):
                caps = parse_getcap(self.rec.run("criu-getcap", ["getcap", self.criu],
                                                 timeout=15).stdout())
            scope = read_text("/proc/sys/kernel/yama/ptrace_scope")
            facts.update({"file_caps": sorted(caps), "yama_ptrace_scope": scope})
            missing = []
            if not caps & {"cap_checkpoint_restore", "cap_sys_admin"}:
                missing.append("cap_checkpoint_restore")
            if "cap_sys_ptrace" not in caps and scope != "0":
                missing.append(f"cap_sys_ptrace (ptrace_scope={scope})")
            if missing:
                return Check("backend available", Status.BLOCKED,
                             "unprivileged CRIU needs file capabilities it does not have: "
                             + ", ".join(missing) + " (granting them needs approval; see report)",
                             facts=facts)
        check = self.rec.run("criu-check", [*criu_prefix(self.mode), self.criu, "check",
                                            *criu_global(self.mode)], timeout=60)
        facts["criu_check"] = (check.stdout() + check.stderr()).strip().splitlines()[-5:]
        facts["criu_check_exit"] = check.exit_code
        return Check("backend available", Status.PASS,
                     f"{self.version} ({self.mode}); criu check exit {check.exit_code}", facts=facts)

    def launch(self, workload: Sequence[str]) -> Session:
        # No tty and no open log files: stdin/stdout/stderr are /dev/null.
        wl = self.rec.spawn("criu-workload", list(workload), capture=False)
        return Session(wl)

    def _own(self, path: Path) -> None:
        if self.mode == "sudo":
            self.rec.run("chown-images", ["sudo", "-n", "chown", "-R",
                                          f"{os.getuid()}:{os.getgid()}", str(path)], timeout=60)

    def checkpoint(self, session: Session,
                   audit: Callable[[str], None] = lambda step: None) -> tuple[Check, ArtifactSet]:
        assert self.criu is not None
        self.images.mkdir(exist_ok=True, mode=0o700)
        log = self.rec.logs / "criu-dump.log"
        target = session.workload.ident
        if not alive(target):
            raise BackendStepError(f"refusing to dump pid {target.pid}: not the process the "
                                   "harness launched (identity changed or process gone)")
        res = self.rec.run("criu-dump", criu_dump_argv(self.criu, self.mode,
                                                       session.workload.ident.pid, self.images, log),
                           timeout=180)
        self._own(self.images)
        self._own(log)
        artifacts = collect_artifacts(self.images, self.rec.run_dir, ["*.img"])
        facts = {"duration_s": round(res.duration_s, 3), "image_bytes": artifacts.total_bytes,
                 "image_files": len(artifacts.items), "log": str(log)}
        if not res.ok:
            return Check("native checkpoint created", Status.FAIL,
                         f"{res.summary()} (see {log})", facts=facts), artifacts
        if not artifacts.items:
            return Check("native checkpoint created", Status.FAIL,
                         "criu dump succeeded but produced no *.img files", facts=facts), artifacts
        return Check("native checkpoint created", Status.PASS,
                     f"{len(artifacts.items)} CRIU image files, {artifacts.total_bytes} bytes, "
                     f"{res.duration_s:.2f}s", facts=facts), artifacts

    def stop_original(self, session: Session) -> bool:
        return self.rec.terminate(session.workload, sig=signal.SIGKILL)

    def restore(self, images: Sequence[Path], not_before_ticks: int) -> tuple[Check, Session | None]:
        assert self.criu is not None
        log = self.rec.logs / "criu-restore.log"
        pidfile = self.rec.run_dir / "restored.pid"
        res = self.rec.run("criu-restore",
                           criu_restore_argv(self.criu, self.mode, self.images, log, pidfile),
                           timeout=180)
        self._own(log)
        if not res.ok:
            return Check("restore command started", Status.FAIL, f"{res.summary()} (see {log})"), None
        text = read_text(str(pidfile), "")
        if not text.isdigit():
            return Check("restore command started", Status.FAIL, "criu restore wrote no pidfile"), None
        tracked = self.rec.adopt("criu-restored", int(text), not_before_ticks)
        if tracked is None:
            return Check("restore command started", Status.FAIL,
                         f"restored pid {text} not found or not owned by this user"), None
        return (Check("restore command started", Status.PASS,
                      f"criu restore exited 0 with root pid {text}; restoration NOT yet verified "
                      "(see state continuity)"), Session(tracked))

    def restored_pid(self, kpid: int | None, pid: int) -> int | None:
        return kpid if kpid is not None else pid  # CRIU does not virtualize PIDs

    def cleanup(self, sessions: Sequence[Session]) -> None:
        return None

    def image_paths(self, artifacts: ArtifactSet) -> list[Path]:
        return [self.images]


class BackendStepError(RuntimeError):
    pass


Backend = Dmtcp | Criu


def tool_version(argv: Sequence[str]) -> str:
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unavailable ({exc})"
    text = (done.stdout + done.stderr).strip()
    return text.splitlines()[0] if text else f"exit {done.returncode}"
