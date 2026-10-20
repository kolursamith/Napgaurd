"""Process identity, ownership, and cleanup (SEC-003, SEC-004, LIM-006).

Reviewed port of the Stage 02 harness (`experiments/stage02/ngexp/runner.py`), whose
mechanisms were verified on Ubuntu 24.04 aarch64. The rules:

* every process starts from an argument array (never a shell), with stdin from
  /dev/null, a closed descriptor set, an allow-listed environment, umask 077, and its
  own session, and every command has a timeout;
* a process is identified by (pid, kernel start time, uid) plus the boot ID, and is
  signalled through a pidfd only after that identity is re-checked;
* a process group is signalled only while its leader is our own unreaped child: the
  leader (alive or zombie) keeps the PID/PGID allocated, so the group is ours;
* a process we did not spawn (a restored workload) is adopted only if it belongs to
  this user AND to a session we created, and started no earlier than a given time;
* the leak scan only REPORTS processes; it never chooses what to kill.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# SEC-008: what a managed process inherits from the caller's environment by default.
ALLOWED_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ")
MAX_LOG_TAIL_BYTES = 64 * 1024


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def filtered_env(base: Mapping[str, str] | None = None,
                 extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Allow-listed environment (SEC-008). `extra` entries are added explicitly."""
    source = os.environ if base is None else base
    env = {key: source[key] for key in ALLOWED_ENV if key in source}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    if extra:
        env.update(extra)
    return env


# ------------------------------------------------------------------ identity


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
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError,
            NotADirectoryError):
        return None
    uid = next(int(line.split()[1]) for line in status.splitlines() if line.startswith("Uid:"))
    return ProcInfo(ProcId(pid, stat.start_ticks, uid), stat.state, stat.pgid, stat.sid)


def alive(ident: ProcId) -> bool:
    current = read_info(ident.pid)
    return current is not None and current.ident == ident and current.running


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def boot_ticks_now() -> int:
    """Current time in the clock-tick units of /proc/<pid>/stat field 22 (since boot)."""
    uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    return int(uptime * os.sysconf("SC_CLK_TCK"))


def signal_identity(ident: ProcId, sig: int) -> bool:
    """Signal exactly the process with this identity, via a pidfd. False if it is gone."""
    try:
        pidfd = os.pidfd_open(ident.pid)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    try:
        if not alive(ident):  # the pidfd pins one process; verify it is ours
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


_CLD_KINDS = {getattr(os, n): n for n in ("CLD_EXITED", "CLD_KILLED", "CLD_DUMPED")
              if hasattr(os, n)}


def child_exit_status(popen: subprocess.Popen[bytes]) -> dict[str, object] | None:
    """How our exited child ended (exit code or signal), WITHOUT reaping it.

    None while the child is running, or when it is not (or no longer) our child.
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


def all_processes() -> list[ProcInfo]:
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            info = read_info(int(entry.name))
            if info is not None:
                found.append(info)
    return found


def running_group_members(pgid: int) -> list[ProcInfo]:
    """Non-zombie processes whose process group is `pgid`."""
    return [p for p in all_processes() if p.pgid == pgid and p.running]


def fd_links(pid: int) -> list[tuple[int, str]]:
    """(fd, readlink target) for every descriptor of `pid` (same-user processes only)."""
    found = []
    for entry in Path(f"/proc/{pid}/fd").iterdir():
        try:
            found.append((int(entry.name), os.readlink(entry)))
        except (OSError, ValueError):
            continue  # descriptor closed while listing
    return sorted(found)


def mapped_paths(pid: int) -> set[str]:
    """File paths mapped into `pid` (from /proc/<pid>/maps)."""
    paths = set()
    for line in Path(f"/proc/{pid}/maps").read_text(encoding="utf-8",
                                                     errors="replace").splitlines():
        parts = line.split(maxsplit=5)
        if len(parts) == 6 and parts[5].startswith("/"):
            paths.add(parts[5].strip())
    return paths


def other_user_processes(own_uid: int | None = None, min_uid: int = 1000,
                         ignored_uids: Sequence[int] = (65534,),
                         processes: Callable[[], list[ProcInfo]] = all_processes) -> list[ProcInfo]:
    """Running processes of OTHER regular users (uid >= min_uid). 65534 (nobody) is a
    system account and is ignored; system service accounts (uid < 1000) are not counted."""
    uid = os.getuid() if own_uid is None else own_uid
    return [p for p in processes() if p.running and p.ident.uid >= min_uid
            and p.ident.uid != uid and p.ident.uid not in ignored_uids]


def leaked_processes(marker: Path, exclude: Sequence[int] = ()) -> list[ProcInfo]:
    """This user's live processes whose cmdline or mappings mention `marker`.

    Used only to REPORT leaks, never to choose kill targets.
    """
    needle = str(marker.resolve()).encode()
    skip = {os.getpid(), *exclude}
    found = []
    for info in all_processes():
        if info.ident.pid in skip or not info.running or info.ident.uid != os.getuid():
            continue
        for name in ("cmdline", "maps"):
            try:
                if needle in Path(f"/proc/{info.ident.pid}/{name}").read_bytes():
                    found.append(info)
                    break
            except OSError:
                continue
    return found


def wait_until(predicate: Callable[[], bool], timeout: float, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def log_tail(path: Path | None, lines: int = 60) -> list[str]:
    """The last `lines` lines of a captured log, unfiltered and size-capped."""
    if path is None or not path.is_file() or path.is_symlink():
        return []
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - MAX_LOG_TAIL_BYTES))
        text = fh.read().decode("utf-8", errors="replace")
    return [line.rstrip() for line in text.splitlines()[-lines:]]


# ------------------------------------------------------------------ tracker


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
        return "\n".join(log_tail(Path(self.stdout_path), 400))

    def stderr(self) -> str:
        return "\n".join(log_tail(Path(self.stderr_path), 400))

    def summary(self) -> str:
        tail = (self.stderr() or self.stdout()).strip().splitlines()[-3:]
        state = "timed out" if self.timed_out else f"exit {self.exit_code}"
        return f"{self.name}: {state} in {self.duration_s:.2f}s" + (
            f" — {' | '.join(tail)}" if tail and not self.ok else "")


@dataclass
class ManagedProcess:
    name: str
    ident: ProcId
    popen: subprocess.Popen[bytes] | None = None
    own_group: bool = False  # True when we created this process's session
    stdout_path: Path | None = None
    stderr_path: Path | None = None


def _private_log(path: Path) -> int:
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
                   0o600)


class ProcessTracker:
    """Starts, adopts, and stops processes; records each one for cleanup and audit."""

    def __init__(self, log_dir: Path, env: Mapping[str, str] | None = None) -> None:
        self.log_dir = log_dir
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.env = dict(env) if env is not None else filtered_env()
        self.processes: list[ManagedProcess] = []
        self.sessions: set[int] = set()  # start_new_session => session id == leader pid
        self.boot_id = boot_id()
        self._seq = len(list(log_dir.glob("*.err")))

    def _next(self, name: str) -> tuple[Path, Path]:
        self._seq += 1
        stem = f"{self._seq:03d}-{name}"
        return self.log_dir / f"{stem}.out", self.log_dir / f"{stem}.err"

    def event(self, event: str, **data: Any) -> None:
        fd = _private_log(self.log_dir / "events.jsonl")
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": utc_now(), "event": event, **data}, sort_keys=True,
                                default=str) + "\n")

    def _popen(self, argv: Sequence[str], out: int, err: int, cwd: Path | None,
               pass_fds: tuple[int, ...]) -> subprocess.Popen[bytes]:
        return subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                cwd=cwd, env=self.env, start_new_session=True, close_fds=True,
                                pass_fds=pass_fds, umask=0o077)

    def run(self, name: str, argv: Sequence[str], *, timeout: float,
            cwd: Path | None = None) -> CmdResult:
        """Run a command to completion with a timeout (REL-008); the group dies on timeout."""
        out_path, err_path = self._next(name)
        out, err = _private_log(out_path), _private_log(err_path)
        started = time.monotonic()
        timed_out = False
        try:
            proc = self._popen(argv, out, err, cwd, ())
        finally:
            os.close(out)
            os.close(err)
        try:
            code = proc.wait(timeout=max(0.1, timeout))
        except subprocess.TimeoutExpired:
            timed_out = True
            _killpg_quietly(proc.pid, signal.SIGKILL)  # unreaped child: the group is ours
            code = proc.wait()
        result = CmdResult(name, list(argv), code, timed_out, time.monotonic() - started,
                           str(out_path), str(err_path))
        self.event("run", **asdict(result))
        return result

    def spawn(self, name: str, argv: Sequence[str], *, cwd: Path | None = None,
              fd_map: tuple[int, int] | None = None) -> ManagedProcess:
        """Start a background process in its own session and record its identity.

        `fd_map=(source_fd, target_fd)` makes `source_fd` available in the child as
        `target_fd`; no other extra descriptor is inherited.
        """
        out_path, err_path = self._next(name)
        out, err = _private_log(out_path), _private_log(err_path)
        pass_fds: tuple[int, ...] = ()
        try:
            if fd_map is not None:
                source, target = fd_map
                try:
                    os.fstat(target)
                except OSError:
                    pass  # the target number is free here, as required
                else:
                    raise RuntimeError(f"fd {target} is already in use")
                os.dup2(source, target, inheritable=True)
                pass_fds = (target,)
            try:
                proc = self._popen(argv, out, err, cwd, pass_fds)
            finally:
                if fd_map is not None:
                    os.close(fd_map[1])  # the child keeps its copy; the source stays open
        finally:
            os.close(out)
            os.close(err)
        ident = _identity_soon(proc.pid)
        self.sessions.add(proc.pid)
        managed = ManagedProcess(name, ident, proc, own_group=True, stdout_path=out_path,
                                 stderr_path=err_path)
        self.processes.append(managed)
        self.event("spawn", name=name, argv=list(argv), pid=proc.pid,
                   start_ticks=ident.start_ticks)
        return managed

    def adopt(self, name: str, pid: int, not_before_ticks: int | None = None
              ) -> ManagedProcess | None:
        """Track a process we did not spawn (e.g. a restored workload), if it is ours."""
        found = read_info(pid)
        reason = None
        if found is None or not found.running:
            reason = "not running"
        elif found.ident.uid != os.getuid():
            reason = f"uid {found.ident.uid}"
        elif found.sid not in self.sessions:
            reason = f"session {found.sid} not created by this tracker"
        elif not_before_ticks is not None and found.ident.start_ticks < not_before_ticks:
            reason = f"started at tick {found.ident.start_ticks}, before {not_before_ticks}"
        if reason is not None or found is None:
            self.event("adopt_refused", name=name, pid=pid, reason=reason)
            return None
        for existing in self.processes:
            if existing.ident == found.ident:
                return existing
        managed = ManagedProcess(name, found.ident)
        self.processes.append(managed)
        self.event("adopted", name=name, pid=pid, start_ticks=found.ident.start_ticks)
        return managed

    def terminate(self, managed: ManagedProcess, grace: float = 3.0,
                  sig: int = signal.SIGTERM) -> bool:
        """Stop one process; for a spawned session leader, its whole group. True if gone."""
        signals: list[int] = [sig, signal.SIGKILL] if sig != signal.SIGKILL else [signal.SIGKILL]
        if managed.popen is not None and managed.own_group:
            gone = _terminate_group(managed.popen, managed.ident.pid, signals, grace)
        else:
            gone = terminate_identity(managed.ident, signals, grace)
        if not gone:
            self.event("terminate_failed", name=managed.name, pid=managed.ident.pid)
        return gone

    def cleanup(self) -> list[str]:
        """Stop every recorded process still alive; return the names that survived."""
        return [f"{m.name}(pid {m.ident.pid})" for m in reversed(self.processes)
                if not self.terminate(m)]

    def records(self) -> list[dict[str, Any]]:
        """Identity records for a session state file (crash cleanup)."""
        return [{"name": m.name, "pid": m.ident.pid, "start_ticks": m.ident.start_ticks,
                 "uid": m.ident.uid, "boot_id": self.boot_id} for m in self.processes]


def _terminate_group(popen: subprocess.Popen[bytes], pgid: int, signals: list[int],
                     grace: float) -> bool:
    if popen.returncode is not None:
        return True  # already reaped: the PGID may now belong to someone else; never signal
    # Until popen.wait() below, the unreaped leader keeps `pgid` allocated.
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


def terminate_identity(ident: ProcId, signals: Sequence[int] = (signal.SIGTERM, signal.SIGKILL),
                       grace: float = 3.0) -> bool:
    """Signal one process by verified identity (pidfd) until it is gone."""
    for current in signals:
        if not alive(ident):
            return True
        signal_identity(ident, current)
        wait_until(lambda: not alive(ident), timeout=grace, interval=0.05)
    return not alive(ident)


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


@dataclass
class SessionState:
    """What `napguard-dev backend cleanup` needs after a crash: identities, not PIDs."""

    session_dir: str
    port: int | None = None
    processes: list[dict[str, Any]] = field(default_factory=list)

    def write(self, path: Path) -> None:
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    @classmethod
    def read(cls, path: Path) -> SessionState:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding="utf-8") as fh:
            data = json.load(fh)
        return cls(str(data["session_dir"]), data.get("port"), list(data.get("processes", [])))


def cleanup_recorded(state: SessionState) -> tuple[list[str], list[str]]:
    """Stop recorded processes that still match their identity in THIS boot.

    Returns (stopped, skipped). A record from another boot, another user, or whose
    identity no longer matches is skipped: nothing is ever signalled by PID alone.
    """
    stopped, skipped = [], []
    current_boot = boot_id()
    for rec in state.processes:
        label = f"{rec.get('name')}(pid {rec.get('pid')})"
        try:
            ident = ProcId(int(rec["pid"]), int(rec["start_ticks"]), int(rec["uid"]))
        except (KeyError, TypeError, ValueError):
            skipped.append(f"{label}: malformed record")
            continue
        if rec.get("boot_id") != current_boot:
            skipped.append(f"{label}: recorded in another boot")
        elif ident.uid != os.getuid():
            skipped.append(f"{label}: other user")
        elif not alive(ident):
            skipped.append(f"{label}: not running (or identity changed)")
        elif terminate_identity(ident):
            stopped.append(label)
        else:
            skipped.append(f"{label}: still alive after SIGKILL")
    return stopped, skipped
