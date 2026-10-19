"""DMTCP and CRIU experiment drivers (Stage 02 only; not the Stage 03 adapter).

Each driver exposes the same steps the trial needs: probe, launch, checkpoint,
stop the original, restore, and identify the restored processes. Argument
vectors are built by pure functions so they can be unit-tested on any host.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .results import Check, Status
from .runner import (
    ArtifactSet,
    Recorder,
    Tracked,
    alive,
    collect_artifacts,
    leader_exited_unreaped,
    read_text,
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


def listeners_loopback_only(ss_output: str) -> bool:
    """True if `ss -Hltn` output lists at least one listener and all are on loopback."""
    locals_ = [line.split()[3] for line in ss_output.splitlines() if len(line.split()) >= 4]
    if not locals_:
        return False
    return all(addr.startswith(("127.", "[::1]:", "::1:")) for addr in locals_)


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


def dmtcp_coordinator_argv(tools: DmtcpTools, port_file: Path, ckpt_dir: Path) -> list[str]:
    # Port 0 = kernel-assigned free port, written to port_file.
    return [tools.coordinator, "--coord-port", "0", "--port-file", str(port_file),
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

    def __init__(self, rec: Recorder, prefix: Path | None) -> None:
        self.rec = rec
        self.tools = find_dmtcp(prefix)
        self.ckpt_dir = rec.run_dir / "checkpoint"
        self.tmp_dir = rec.run_dir / "dmtcp-tmp"
        self.version = "unknown"

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
        if not DMTCP_LOOPBACK_ONLY_COORDINATOR_VERIFIED:
            return Check("backend available", Status.BLOCKED,
                         f"DMTCP {self.version} installed, but {DMTCP_COORDINATOR_BLOCKER}",
                         facts=facts)
        return Check("backend available", Status.PASS, f"DMTCP {self.version}", facts=facts)

    def _coordinator(self, label: str) -> tuple[Tracked, int]:
        assert self.tools is not None
        if not DMTCP_LOOPBACK_ONLY_COORDINATOR_VERIFIED:
            raise BackendStepError(DMTCP_COORDINATOR_BLOCKER)  # nothing has been spawned
        port_file = self.rec.run_dir / f"{label}.port"
        self.ckpt_dir.mkdir(exist_ok=True, mode=0o700)
        coord = self.rec.spawn(label, dmtcp_coordinator_argv(self.tools, port_file, self.ckpt_dir),
                               cwd=self.ckpt_dir)
        ok = wait_until(lambda: read_text(str(port_file), "").isdigit(), timeout=15)
        if not ok:
            raise BackendStepError(f"{label} did not publish its port within 15 s")
        port = int(read_text(str(port_file)))
        listening = self.rec.run(f"{label}-listen", ["ss", "-Hltn", f"sport = :{port}"], timeout=10)
        self.rec.event("dmtcp_coordinator", label=label, port=port,
                       listen=listening.stdout().strip())
        if not (listening.ok and listeners_loopback_only(listening.stdout())):
            self.rec.terminate(coord, sig=signal.SIGKILL)
            raise BackendStepError(f"{label} is not loopback-only: {listening.stdout().strip()!r}")
        return coord, port

    def launch(self, workload: Sequence[str]) -> Session:
        assert self.tools is not None
        self.tmp_dir.mkdir(exist_ok=True, mode=0o700)
        coord, port = self._coordinator("coordinator")
        wl = self.rec.spawn(
            "dmtcp-launch", dmtcp_launch_argv(self.tools, port, self.ckpt_dir, self.tmp_dir, workload)
        )
        return Session(wl, [coord], {"port": port})

    def checkpoint(self, session: Session) -> tuple[Check, ArtifactSet]:
        assert self.tools is not None
        port = int(session.data["port"])
        self.rec.run("dmtcp-list-before", dmtcp_command_argv(self.tools, port, "--list"), timeout=15)
        res = self.rec.run("dmtcp-checkpoint", dmtcp_command_argv(self.tools, port, "--bcheckpoint"),
                           timeout=180)
        artifacts = collect_artifacts(self.ckpt_dir, self.rec.run_dir,
                                      ["ckpt_*.dmtcp", "dmtcp_restart_script*.sh"])
        images = [a for a in artifacts.items if a.path.endswith(".dmtcp") and a.size > 0]
        facts = {"duration_s": round(res.duration_s, 3), "image_bytes": artifacts.total_bytes,
                 "images": [a.path for a in images]}
        if not res.ok:
            return Check("native checkpoint created", Status.FAIL, res.summary(), facts=facts), artifacts
        if not images:
            return Check("native checkpoint created", Status.FAIL,
                         "dmtcp_command reported success but no ckpt_*.dmtcp image exists",
                         facts=facts), artifacts
        return Check("native checkpoint created", Status.PASS,
                     f"{len(images)} DMTCP image(s), {artifacts.total_bytes} bytes, "
                     f"{res.duration_s:.2f}s", facts=facts), artifacts

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
            return Check("restore command", Status.FAIL, "dmtcp_restart exited immediately"), session
        return Check("restore command", Status.PASS, "dmtcp_restart running"), session

    def restored_pid(self, kpid: int | None, pid: int) -> int | None:
        return kpid  # DMTCP virtualizes getpid(); only the kernel PID identifies the process

    def cleanup(self, sessions: Sequence[Session]) -> None:
        if self.tools is None:
            return
        for session in sessions:
            if "port" in session.data:
                self.rec.run("dmtcp-quit", dmtcp_command_argv(self.tools, int(session.data["port"]),
                                                               "--quit"), timeout=15)

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

    def checkpoint(self, session: Session) -> tuple[Check, ArtifactSet]:
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
            return Check("restore command", Status.FAIL, f"{res.summary()} (see {log})"), None
        text = read_text(str(pidfile), "")
        if not text.isdigit():
            return Check("restore command", Status.FAIL, "criu restore wrote no pidfile"), None
        tracked = self.rec.adopt("criu-restored", int(text), not_before_ticks)
        if tracked is None:
            return Check("restore command", Status.FAIL,
                         f"restored pid {text} not found or not owned by this user"), None
        return Check("restore command", Status.PASS, f"criu restored root pid {text}"), Session(tracked)

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
