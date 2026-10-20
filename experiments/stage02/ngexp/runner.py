"""Linux process, command, and evidence plumbing for Stage 02 experiments.

Safety rules enforced here:

* every command runs with an argument array (no shell), a timeout, and its own
  session, so a timeout kills only that command's process group;
* every command line, exit status, duration, and output file is recorded in
  ``commands.jsonl`` inside the run directory;
* a process is signalled only if its (pid, kernel start time, uid) identity
  matches the identity recorded when the harness created or adopted it, so a
  reused PID or an unrelated user process is never touched;
* a process group is signalled only while its leader is this harness's own
  child and not yet reaped: the leader (alive or zombie) keeps the PID/PGID
  number allocated, so the group cannot belong to anyone else;
* a process the harness did not spawn (a restored workload) is adopted only if
  it is owned by this user AND belongs to a session this run created, and it is
  signalled through a pidfd after re-checking its identity.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(frozen=True)
class ProcId:
    pid: int
    start_ticks: int
    uid: int


@dataclass(frozen=True)
class StatFields:
    state: str
    pgid: int
    sid: int
    start_ticks: int


def parse_stat(text: str) -> StatFields:
    """Fields of /proc/<pid>/stat. comm (field 2) may contain spaces and ')'."""
    after_comm = text[text.rindex(")") + 2 :].split()
    # after_comm[0] is field 3 (state); field N is after_comm[N - 3].
    return StatFields(after_comm[0], int(after_comm[2]), int(after_comm[3]), int(after_comm[19]))


@dataclass(frozen=True)
class ProcInfo:
    ident: ProcId
    state: str
    pgid: int
    sid: int

    @property
    def running(self) -> bool:
        return self.state not in ("Z", "X")


def read_info(pid: int) -> ProcInfo | None:
    """Identity, state, process group, and session from /proc; None if gone."""
    try:
        stat = parse_stat(Path(f"/proc/{pid}/stat").read_text(encoding="utf-8"))
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError):
        return None
    uid = next(int(line.split()[1]) for line in status.splitlines() if line.startswith("Uid:"))
    return ProcInfo(ProcId(pid, stat.start_ticks, uid), stat.state, stat.pgid, stat.sid)


def alive(ident: ProcId) -> bool:
    current = read_info(ident.pid)
    return current is not None and current.ident == ident and current.running


def signal_identity(ident: ProcId, sig: int) -> bool:
    """Signal exactly the process with this identity, via a pidfd. False if it is gone."""
    try:
        pidfd = os.pidfd_open(ident.pid)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    try:
        # The pidfd pins one process; verify that this pinned process is ours.
        if not alive(ident):
            return False
        signal.pidfd_send_signal(pidfd, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    finally:
        os.close(pidfd)


def leader_exited_unreaped(popen: subprocess.Popen[bytes]) -> bool:
    """True if our child has exited but is not reaped (still holds its PID/PGID)."""
    try:
        info = os.waitid(os.P_PID, popen.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return False
    return info is not None


_CLD_KINDS = {getattr(os, n): n for n in ("CLD_EXITED", "CLD_KILLED", "CLD_DUMPED") if hasattr(os, n)}


def child_exit_status(popen: subprocess.Popen[bytes]) -> dict[str, object] | None:
    """How our exited child ended (exit code or signal), WITHOUT reaping it (diagnostics).

    None while the child is running or when it is not (or no longer) our child.
    """
    try:
        info = os.waitid(os.P_PID, popen.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return None
    if info is None:
        return None
    kind = _CLD_KINDS.get(info.si_code, str(info.si_code))
    status: dict[str, object] = {"pid": info.si_pid, "how": kind}
    if kind == "CLD_EXITED":
        status["exit_code"] = info.si_status
    else:
        status["signal"] = info.si_status
        try:
            status["signal_name"] = signal.Signals(info.si_status).name
        except ValueError:
            pass
        status["core_dumped"] = kind == "CLD_DUMPED"
    return status


@dataclass
class CmdResult:
    name: str
    argv: list[str]
    exit_code: int
    timed_out: bool
    duration_s: float
    stdout_path: str
    stderr_path: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def stdout(self) -> str:
        return Path(self.stdout_path).read_text(encoding="utf-8", errors="replace")

    def stderr(self) -> str:
        return Path(self.stderr_path).read_text(encoding="utf-8", errors="replace")

    def summary(self) -> str:
        tail = (self.stderr() or self.stdout()).strip().splitlines()[-3:]
        state = "timed out" if self.timed_out else f"exit {self.exit_code}"
        return f"{self.name}: {state} in {self.duration_s:.2f}s" + (
            f" — {' | '.join(tail)}" if tail and not self.ok else ""
        )


@dataclass
class Tracked:
    name: str
    ident: ProcId
    popen: subprocess.Popen[bytes] | None = None
    own_group: bool = False  # True when the harness created this process's session
    stdout_path: Path | None = None
    stderr_path: Path | None = None


class Recorder:
    """Records commands/events and tracks every process the harness creates."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.logs = run_dir / "logs"
        self.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Continue numbering when a run directory is reused (reboot-resume keeps earlier logs).
        self._seq = len(list(self.logs.glob("*.out")))
        self.tracked: list[Tracked] = []
        # Sessions created by this run (start_new_session => session id == leader pid).
        self.sessions: set[int] = set()

    def _next(self, name: str) -> tuple[Path, Path]:
        self._seq += 1
        stem = f"{self._seq:03d}-{name}"
        return self.logs / f"{stem}.out", self.logs / f"{stem}.err"

    def _append(self, filename: str, record: Mapping[str, Any]) -> None:
        with (self.run_dir / filename).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")

    def event(self, event: str, **data: Any) -> None:
        self._append("events.jsonl", {"ts": utc_now(), "event": event, **data})
        print(f"[{event}] " + ", ".join(f"{k}={v}" for k, v in data.items()), flush=True)

    def run(
        self,
        name: str,
        argv: Sequence[str],
        *,
        timeout: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CmdResult:
        out_path, err_path = self._next(name)
        print("$ " + " ".join(argv), flush=True)
        started = time.monotonic()
        timed_out = False
        with out_path.open("wb") as out, err_path.open("wb") as err:
            proc = subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                cwd=cwd,
                env=dict(env) if env is not None else None,
                start_new_session=True,
            )
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _killpg_quietly(proc.pid, signal.SIGKILL)
                code = proc.wait()
        result = CmdResult(
            name, list(argv), code, timed_out, time.monotonic() - started, str(out_path), str(err_path)
        )
        self._append("commands.jsonl", {"ts": utc_now(), "kind": "run", **result.__dict__})
        print(f"  -> {'TIMEOUT' if timed_out else f'exit {code}'} ({result.duration_s:.2f}s)", flush=True)
        return result

    def spawn(
        self,
        name: str,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        capture: bool = True,
        fd_map: tuple[int, int] | None = None,
    ) -> Tracked:
        """Start a background process in its own session and track its identity.

        `fd_map=(source_fd, target_fd)` makes `source_fd` available in the child as
        `target_fd` (and no other extra descriptor is inherited).
        """
        out_path, err_path = self._next(name)
        print("$ " + " ".join(argv) + "   (background)", flush=True)
        out: IO[bytes] | int
        err: IO[bytes] | int
        if capture:
            out = out_path.open("ab")
            err = err_path.open("ab")
        else:
            out = err = subprocess.DEVNULL
        pass_fds: tuple[int, ...] = ()
        if fd_map is not None:
            source, target = fd_map
            try:
                os.fstat(target)
            except OSError:
                pass  # target number is free in the harness, as required
            else:
                raise RuntimeError(f"fd {target} is already in use in the harness")
            os.dup2(source, target, inheritable=True)
            pass_fds = (target,)
        try:
            proc = subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                cwd=cwd,
                env=dict(env) if env is not None else None,
                start_new_session=True,
                pass_fds=pass_fds,
            )
        finally:
            for fh in (out, err):
                if not isinstance(fh, int):
                    fh.close()
            if fd_map is not None:
                os.close(fd_map[1])  # the child keeps its copy; the source fd stays open
        ident = _identity_soon(proc.pid)
        self.sessions.add(proc.pid)
        tracked = Tracked(name, ident, proc, own_group=True,
                          stdout_path=out_path if capture else None,
                          stderr_path=err_path if capture else None)
        self.tracked.append(tracked)
        self._append(
            "commands.jsonl",
            {
                "ts": utc_now(),
                "kind": "spawn",
                "name": name,
                "argv": list(argv),
                "pid": proc.pid,
                "start_ticks": ident.start_ticks,
                "stdout_path": str(out_path) if capture else None,
                "stderr_path": str(err_path) if capture else None,
            },
        )
        return tracked

    def adopt(self, name: str, pid: int, not_before_ticks: int | None = None) -> Tracked | None:
        """Track a process the harness did not spawn (e.g. a restored workload).

        Adopted only if owned by this user, in a session this run created (a restored
        tree keeps, or is started in, the experiment's session), and — when
        `not_before_ticks` is given — started no earlier than that boot-relative time
        (e.g. the moment the restore command began).
        """
        found = read_info(pid)
        if found is None or not found.running:
            self.event("adopt_refused", name=name, pid=pid, reason="not running")
            return None
        if found.ident.uid != os.getuid():
            self.event("adopt_refused", name=name, pid=pid, reason=f"uid {found.ident.uid}")
            return None
        if found.sid not in self.sessions:
            self.event("adopt_refused", name=name, pid=pid,
                       reason=f"session {found.sid} not created by this run")
            return None
        if not_before_ticks is not None and found.ident.start_ticks < not_before_ticks:
            self.event("adopt_refused", name=name, pid=pid,
                       reason=f"started at tick {found.ident.start_ticks}, before {not_before_ticks}")
            return None
        for existing in self.tracked:
            if existing.ident == found.ident:
                return existing
        tracked = Tracked(name, found.ident)
        self.tracked.append(tracked)
        self.event("adopted", name=name, pid=pid, start_ticks=found.ident.start_ticks,
                   sid=found.sid)
        return tracked

    def terminate(self, tracked: Tracked, grace: float = 3.0, sig: int = signal.SIGTERM) -> bool:
        """Stop one tracked process; for a spawned session leader, its whole group. True if gone."""
        signals: list[int] = [sig, signal.SIGKILL] if sig != signal.SIGKILL else [signal.SIGKILL]
        if tracked.popen is not None and tracked.own_group:
            gone = self._terminate_group(tracked.popen, tracked.ident.pid, signals, grace)
        else:
            gone = self._terminate_one(tracked.ident, signals, grace)
        if not gone:
            self.event("terminate_failed", name=tracked.name, pid=tracked.ident.pid)
        return gone

    def _terminate_group(self, popen: subprocess.Popen[bytes], pgid: int, signals: list[int],
                         grace: float) -> bool:
        if popen.returncode is not None:
            # Already reaped: the PGID number may now belong to someone else. Never signal it.
            return True
        # From here until popen.wait() below, the unreaped leader keeps `pgid` allocated,
        # so every member of process group `pgid` was started inside this run's session.
        for current in signals:
            if not running_group_members(pgid):
                break
            _killpg_quietly(pgid, current)
            wait_until(lambda: not running_group_members(pgid), timeout=grace, interval=0.05)
        gone = not running_group_members(pgid)
        if gone:
            try:
                popen.wait(timeout=5)
            except subprocess.TimeoutExpired:
                gone = False
        return gone

    @staticmethod
    def _terminate_one(ident: ProcId, signals: list[int], grace: float) -> bool:
        for current in signals:
            if not alive(ident):
                return True
            signal_identity(ident, current)
            wait_until(lambda: not alive(ident), timeout=grace, interval=0.05)
        return not alive(ident)

    def cleanup(self) -> list[str]:
        """Terminate every tracked process still alive; return names that survived."""
        survivors = []
        for tracked in reversed(self.tracked):
            if not self.terminate(tracked):
                survivors.append(f"{tracked.name}(pid {tracked.ident.pid})")
        return survivors


def socket_fds(pid: int) -> list[tuple[int, str]]:
    """(fd, link) for every socket descriptor of `pid` (same-user processes only)."""
    found = []
    for entry in Path(f"/proc/{pid}/fd").iterdir():
        try:
            link = os.readlink(entry)
        except OSError:
            continue  # descriptor closed while listing
        if link.startswith("socket:"):
            found.append((int(entry.name), link))
    return sorted(found)


def boot_ticks_now() -> int:
    """Current time in the clock-tick units of /proc/<pid>/stat field 22 (since boot)."""
    uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    return int(uptime * os.sysconf("SC_CLK_TCK"))


def running_group_members(pgid: int) -> list[ProcInfo]:
    """Non-zombie processes whose process group is `pgid`."""
    members = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            info = read_info(int(entry.name))
            if info is not None and info.pgid == pgid and info.running:
                members.append(info)
    return members


def _identity_soon(pid: int) -> ProcId:
    for _ in range(50):
        found = read_info(pid)
        if found is not None:
            return found.ident
        time.sleep(0.01)
    raise RuntimeError(f"could not read identity of new process {pid}")


def _killpg_quietly(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


# ------------------------------------------------------------- network guard


@dataclass(frozen=True)
class Listener:
    proto: str
    local: str
    pids: tuple[int, ...]

    @property
    def loopback(self) -> bool:
        return self.local.startswith(("127.", "[::1]:", "::1:"))

    @property
    def port(self) -> int:
        return int(self.local.rsplit(":", 1)[1])


def parse_ss_listeners(output: str, proto: str = "tcp") -> list[Listener]:
    """Parse `ss -H -l -n -p` lines: State Recv-Q Send-Q Local Peer [users:((...,pid=N,...))]."""
    found = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        pids = tuple(int(p) for p in re.findall(r"pid=(\d+)", line))
        found.append(Listener(proto, parts[3], pids))
    return found


def listener_violations(listeners: Sequence[Listener], pids: set[int]) -> list[Listener]:
    """Non-loopback listeners owned by any of `pids` (an experiment's processes)."""
    return [lst for lst in listeners if not lst.loopback and set(lst.pids) & pids]


def current_listeners() -> list[Listener]:
    """All TCP and UDP listeners; owners are visible for this user's own processes."""
    found: list[Listener] = []
    for proto, flag in (("tcp", "-t"), ("udp", "-u")):
        done = subprocess.run(["ss", "-H", "-l", "-n", "-p", flag], capture_output=True,
                              text=True, timeout=10)
        if done.returncode != 0:
            raise RuntimeError(f"ss {flag} failed: {done.stderr.strip()}")
        found.extend(parse_ss_listeners(done.stdout, proto))
    return found


def non_loopback_ipv4_addresses() -> list[str]:
    done = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True,
                          timeout=10)
    if done.returncode != 0:  # never silently skip the external-reachability test
        raise RuntimeError(f"ip -o -4 addr show failed: {done.stderr.strip()}")
    addrs = re.findall(r"\binet (\d+\.\d+\.\d+\.\d+)/", done.stdout)
    return [a for a in addrs if not a.startswith("127.")]


def non_loopback_ipv6_addresses() -> list[str]:
    """Global-scope IPv6 addresses (link-local ones need a scope id and are not probed)."""
    done = subprocess.run(["ip", "-o", "-6", "addr", "show", "scope", "global"],
                          capture_output=True, text=True, timeout=10)
    if done.returncode != 0:
        raise RuntimeError(f"ip -o -6 addr show failed: {done.stderr.strip()}")
    return re.findall(r"\binet6 ([0-9a-fA-F:]+)/", done.stdout)


def new_non_loopback(listeners: Sequence[Listener], baseline: set[tuple[str, str]]) -> list[Listener]:
    """Non-loopback listeners (any owner) that were not present in the baseline snapshot."""
    return [lst for lst in listeners if not lst.loopback and (lst.proto, lst.local) not in baseline]


def run_dir_processes(run_dir: Path) -> list[ProcInfo]:
    """This user's live processes (other than the harness) whose cmdline or mappings mention
    `run_dir`. Used only to REPORT leaked experiment processes, never to choose kill targets."""
    marker = str(run_dir.resolve()).encode()
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        info = read_info(int(entry.name))
        if info is None or not info.running or info.ident.uid != os.getuid():
            continue
        for name in ("cmdline", "maps"):
            try:
                if marker in (entry / name).read_bytes():
                    found.append(info)
                    break
            except OSError:
                continue
    return found


def tcp_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def loopback_listener() -> socket.socket:
    """A TCP listener bound to 127.0.0.1 on a kernel-chosen port (no SO_REUSEPORT)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    return sock


# ---------------------------------------------------------------- environment


def fs_type(path: Path) -> str:
    best, best_type = "", "?"
    target = str(path.resolve())
    for line in Path("/proc/self/mounts").read_text(encoding="utf-8").splitlines():
        parts = line.split()
        mount_point = parts[1].replace("\\040", " ")
        under = target == mount_point or target.startswith(mount_point.rstrip("/") + "/")
        if under and len(mount_point) > len(best):
            best, best_type = mount_point, parts[2]
    return best_type


def fs_source(path: Path) -> str:
    best, source = "", "?"
    target = str(path.resolve())
    for line in Path("/proc/self/mounts").read_text(encoding="utf-8").splitlines():
        parts = line.split()
        mount_point = parts[1].replace("\\040", " ")
        under = target == mount_point or target.startswith(mount_point.rstrip("/") + "/")
        if under and len(mount_point) > len(best):
            best, source = mount_point, parts[0]
    return source


PERSISTENT_FS = {"ext4", "xfs", "btrfs"}


def read_text(path: str, default: str = "absent") -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return default


def boot_id() -> str:
    return read_text("/proc/sys/kernel/random/boot_id")


def host_identity() -> dict[str, str]:
    """Facts a restore must match: CPU architecture and (hashed) machine id."""
    machine_id = read_text("/etc/machine-id", "")
    return {"arch": platform.machine(),
            "machine_id_sha256": hashlib.sha256(machine_id.encode()).hexdigest()
            if machine_id else "absent"}


def kernel_config(option: str) -> str:
    config = Path(f"/boot/config-{platform.release()}")
    try:
        for line in config.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{option}=") or line == f"# {option} is not set":
                return line
    except OSError:
        return f"{config} unreadable"
    return f"{option} not mentioned"


def cap_eff() -> str:
    for line in read_text("/proc/self/status", "").splitlines():
        if line.startswith("CapEff:"):
            return line.split()[1]
    return "?"


def environment(run_root: Path) -> dict[str, Any]:
    os_release: dict[str, str] = {}
    for line in read_text("/etc/os-release", "").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            os_release[key] = value.strip('"')
    return {
        "os": os_release.get("PRETTY_NAME", "?"),
        "os_id": os_release.get("ID", "?"),
        "os_version_id": os_release.get("VERSION_ID", "?"),
        "machine": platform.machine(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "uid": os.getuid(),
        "cap_eff": cap_eff(),
        "boot_id": boot_id(),
        "run_root": str(run_root),
        "run_root_fs": fs_type(run_root),
        "run_root_device": fs_source(run_root),
        "yama_ptrace_scope": read_text("/proc/sys/kernel/yama/ptrace_scope"),
        "apparmor_restrict_unprivileged_userns": read_text(
            "/proc/sys/kernel/apparmor_restrict_unprivileged_userns"
        ),
        "ns_last_pid_present": Path("/proc/sys/kernel/ns_last_pid").exists(),
        "pid_max": read_text("/proc/sys/kernel/pid_max"),
        "CONFIG_CHECKPOINT_RESTORE": kernel_config("CONFIG_CHECKPOINT_RESTORE"),
        "CONFIG_MEM_SOFT_DIRTY": kernel_config("CONFIG_MEM_SOFT_DIRTY"),
        "cc": shutil.which("cc") or "absent",
        "getcap": shutil.which("getcap") or "absent",
    }


def git_revision(repo: Path) -> dict[str, str]:
    def git(*args: str) -> str:
        try:
            done = subprocess.run(
                ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=10
            )
        except (OSError, subprocess.TimeoutExpired):
            return "unavailable"
        return done.stdout.strip() if done.returncode == 0 else "unavailable"

    return {"head": git("rev-parse", "HEAD"), "branch": git("branch", "--show-current"),
            "dirty": git("status", "--porcelain")}


# ------------------------------------------------------------------ artifacts


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class Artifact:
    path: str  # relative to the run directory
    size: int
    sha256: str
    mode: str
    is_symlink: bool = False
    target: str | None = None


@dataclass
class ArtifactSet:
    directory: str
    fs: str
    device: str
    items: list[Artifact] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(a.size for a in self.items)


def collect_artifacts(directory: Path, run_dir: Path, patterns: Sequence[str]) -> ArtifactSet:
    found = ArtifactSet(str(directory), fs_type(directory), fs_source(directory))
    seen: set[Path] = set()
    for pattern in patterns:
        for path in sorted(directory.glob(pattern)):
            if path in seen:
                continue
            seen.add(path)
            rel = str(path.relative_to(run_dir))
            if path.is_symlink():
                found.items.append(
                    Artifact(rel, 0, "", "symlink", True, os.readlink(path))
                )
            elif path.is_file():
                st = path.stat()
                found.items.append(
                    Artifact(rel, st.st_size, sha256_file(path), f"{st.st_mode & 0o777:o}")
                )
    return found


def wait_until(predicate: Callable[[], bool], timeout: float, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())
