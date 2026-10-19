#!/usr/bin/env python3
"""NapGuard Stage 01 — verify the Linux assumptions the design depends on.

Run INSIDE Ubuntu as an ordinary user, from the repository root:

    python3 -I scripts/stage01_linux_assumptions.py

Uses only the standard library and the system tools already on Ubuntu.
Side effects are temporary and cleaned up:
  - short-lived child processes (`sleep`) that the script signals itself;
  - a temporary directory under $HOME (filesystem, fsync, and SQLite probes);
  - a 2-second logind *delay* sleep inhibitor (never a block inhibitor);
  - one transient systemd user scope running `true`.
No sudo, no package installation, no system configuration changes, no suspend.

Each check is MANDATORY (a core design assumption) or OPTIONAL (the design
has a documented degraded mode). Exit status: 0 = no mandatory failure,
1 = at least one mandatory check failed.
"""

from __future__ import annotations

import os
import platform
import shutil
import signal
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

Result = tuple[str, str]  # (status, detail)

RESULTS: list[tuple[str, str, str, str]] = []  # (level, status, name, detail)


def run(argv: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    print("$ " + " ".join(argv))
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        print(f"[not found: {argv[0]}]")
        return subprocess.CompletedProcess(argv, 127, "", f"{argv[0]}: not found")
    except subprocess.TimeoutExpired:
        print(f"[timed out after {timeout}s]")
        return subprocess.CompletedProcess(argv, 124, "", "timed out")
    out = (proc.stdout + proc.stderr).rstrip()
    if out:
        print(out)
    print(f"[exit {proc.returncode}]")
    return proc


def check(level: str, name: str, fn: Callable[[], Result]) -> None:
    print(f"### {name}")
    try:
        status, detail = fn()
    except Exception as exc:  # a crashing probe is a failed probe, never a pass
        status, detail = "FAIL", f"{type(exc).__name__}: {exc}"
    RESULTS.append((level, status, name, detail))
    print(f">>> {status:<4}  {level:<9}  {name} — {detail}\n")


def read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


def os_release() -> dict[str, str]:
    data: dict[str, str] = {}
    for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            data[key] = value.strip().strip('"')
    return data


def start_time_ticks(pid: int) -> int:
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    # comm (field 2) may contain spaces/parens: split after the last ')'.
    fields_after_comm = raw[raw.rindex(")") + 2 :].split()
    return int(fields_after_comm[19])  # field 22 overall


def c_platform() -> Result:
    rel = os_release()
    desc = f"{rel.get('PRETTY_NAME', '?')}, {platform.machine()}, kernel {platform.release()}"
    if rel.get("ID") != "ubuntu":
        return "FAIL", f"not Ubuntu: {desc}"
    return "PASS", desc


def c_python() -> Result:
    have = hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")
    ver = platform.python_version()  # >= 3.12 is already enforced by the Stage 00 baseline
    if not have:
        return "FAIL", f"Python {ver} lacks os.pidfd_open / signal.pidfd_send_signal"
    return "PASS", f"Python {ver}, sqlite {sqlite3.sqlite_version}, pidfd API present"


def c_pidfd_and_start_time() -> Result:
    child = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        ticks = start_time_ticks(child.pid)
        pidfd = os.pidfd_open(child.pid)
        try:
            signal.pidfd_send_signal(pidfd, signal.SIGTERM)
        finally:
            os.close(pidfd)
        code = child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    if code != -signal.SIGTERM:
        return "FAIL", f"child exit {code}, expected -SIGTERM"
    return "PASS", f"pidfd_open + pidfd_send_signal ok; /proc start time = {ticks} ticks"


def c_boot_and_machine_id() -> Result:
    boot_id = read_text("/proc/sys/kernel/random/boot_id")
    machine_id = read_text("/etc/machine-id")
    if len(boot_id) != 36 or len(machine_id) != 32:
        return "FAIL", "unexpected boot_id/machine-id format"
    return "PASS", f"boot_id {boot_id}; machine-id readable (value not printed)"


def c_peercred() -> Result:
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        raw = a.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", raw)
    finally:
        a.close()
        b.close()
    if uid != os.getuid() or pid != os.getpid():
        return "FAIL", f"SO_PEERCRED returned pid={pid} uid={uid}"
    return "PASS", f"SO_PEERCRED pid={pid} uid={uid} gid={gid}"


def c_runtime_dir() -> Result:
    path = os.environ.get("XDG_RUNTIME_DIR")
    if not path:
        return "FAIL", "XDG_RUNTIME_DIR is not set (run from a logged-in desktop or console session)"
    st = os.stat(path)
    mode = stat.S_IMODE(st.st_mode)
    if st.st_uid != os.getuid() or mode != 0o700:
        return "FAIL", f"{path} owner uid {st.st_uid}, mode {mode:o} (need own uid, 700)"
    return "PASS", f"{path} owned by uid {st.st_uid}, mode 700"


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


def data_parent() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    path = Path(base)
    while not path.exists():
        path = path.parent
    return path


def c_persistent_fs() -> Result:
    parent = data_parent()
    kind = fs_type(parent)
    if kind in {"tmpfs", "ramfs", "?"}:
        return "FAIL", f"{parent} is on {kind}"
    return "PASS", f"data location {parent} on {kind}"


def c_fsync_rename(tmp: Path) -> Result:
    staging = tmp / "staging" / "op"
    published = tmp / "published"
    staging.mkdir(parents=True, mode=0o700)
    published.mkdir(mode=0o700)
    artifact = staging / "artifact.bin"
    fd = os.open(artifact, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, os.urandom(4096))
        os.fsync(fd)
    finally:
        os.close(fd)
    for d in (staging, staging.parent):
        dfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    os.rename(staging, published / "g000001")
    for d in (published, staging.parent):
        dfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    mode = stat.S_IMODE((published / "g000001" / "artifact.bin").stat().st_mode)
    if mode != 0o600:
        return "FAIL", f"artifact mode {mode:o}, expected 600"
    return "PASS", "file fsync, directory fsync, and same-filesystem rename all succeeded"


def c_sqlite(tmp: Path) -> Result:
    con = sqlite3.connect(tmp / "catalog.sqlite3")
    try:
        mode = con.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        con.execute("PRAGMA synchronous=FULL")
        con.execute("PRAGMA foreign_keys=ON")
        sync = con.execute("PRAGMA synchronous").fetchone()[0]
        fks = con.execute("PRAGMA foreign_keys").fetchone()[0]
        con.execute("CREATE TABLE t (id TEXT PRIMARY KEY, state TEXT, ended_at TEXT)")
        con.execute("CREATE UNIQUE INDEX one_active ON t(state) WHERE ended_at IS NULL")
        with con:
            con.execute("INSERT INTO t VALUES ('a', 'RUNNING', NULL)")
        try:
            with con:
                con.execute("INSERT INTO t VALUES ('b', 'RUNNING', NULL)")
            partial_ok = False
        except sqlite3.IntegrityError:
            partial_ok = True
        check_ok = con.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        con.close()
    detail = (
        f"journal_mode={mode} synchronous={sync} foreign_keys={fks} "
        f"partial_unique_index={'enforced' if partial_ok else 'NOT enforced'} quick_check={check_ok}"
    )
    good = mode == "wal" and sync == 2 and fks == 1 and partial_ok and check_ok == "ok"
    return ("PASS" if good else "FAIL"), detail


def c_user_manager() -> Result:
    proc = run(["systemctl", "--user", "show-environment"])
    if proc.returncode != 0:
        return "FAIL", "systemd user manager not reachable from this session"
    state = run(["systemctl", "--user", "is-system-running"]).stdout.strip()
    return "PASS", f"user manager reachable (state: {state or 'unknown'})"


def c_transient_scope() -> Result:
    proc = run(["systemd-run", "--user", "--scope", "--quiet", "--collect", "true"])
    if proc.returncode != 0:
        return "FAIL", "cannot create a transient user scope (input to OD-03)"
    return "PASS", "transient user scope created and collected (OD-03 option 1 is feasible)"


LOGIND = ["org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager"]


def c_inhibit_delay_max() -> Result:
    proc = run(["busctl", "--system", "get-property", *LOGIND, "InhibitDelayMaxUSec"])
    if proc.returncode != 0:
        return "FAIL", "cannot read InhibitDelayMaxUSec"
    parts = proc.stdout.split()
    usec = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else -1
    if usec < 0:
        return "FAIL", f"unexpected reply: {proc.stdout.strip()}"
    return "PASS", f"InhibitDelayMaxUSec = {usec} us ({usec / 1e6:g} s); pre-suspend budget = this - 1 s"


def c_prepare_for_sleep() -> Result:
    proc = run(["busctl", "--system", "introspect", *LOGIND])
    if proc.returncode != 0 or "PrepareForSleep" not in proc.stdout:
        return "FAIL", "PrepareForSleep signal not found on logind Manager"
    return "PASS", "logind Manager exposes the PrepareForSleep signal"


def c_delay_inhibitor() -> Result:
    who = f"napguard-stage01-{os.getpid()}"
    argv = ["systemd-inhibit", "--what=sleep", "--mode=delay", f"--who={who}",
            "--why=NapGuard Stage 01 probe", "sleep", "2"]
    print("$ " + " ".join(argv) + "  (background)")
    holder = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        time.sleep(0.7)
        listing = run(["systemd-inhibit", "--list", "--no-pager"]).stdout
    finally:
        try:
            out, _ = holder.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            holder.kill()
            out, _ = holder.communicate()
        if out.strip():
            print(out.rstrip())
    if holder.returncode != 0:
        return "FAIL", f"systemd-inhibit exited {holder.returncode} (delay lock not permitted?)"
    if who not in listing:
        return "FAIL", "delay inhibitor was not listed while held"
    return "PASS", "unprivileged user can hold a sleep delay inhibitor; it was listed and released"


def c_power_supply() -> Result:
    root = Path("/sys/class/power_supply")
    entries = sorted(root.iterdir()) if root.exists() else []
    found = []
    for entry in entries:
        kind = read_text(str(entry / "type")) if (entry / "type").exists() else "?"
        found.append(f"{entry.name}:{kind}")
    batteries = [f for f in found if f.endswith(":Battery")]
    if not batteries:
        return "SKIP", f"no battery exposed ({', '.join(found) or 'no entries'}); FR-019 degraded mode applies"
    return "PASS", f"supplies: {', '.join(found)}"


def c_sleep_states() -> Result:
    state = read_text("/sys/power/state") if Path("/sys/power/state").exists() else "missing"
    mem = read_text("/sys/power/mem_sleep") if Path("/sys/power/mem_sleep").exists() else "missing"
    return "PASS", f"/sys/power/state = '{state}', mem_sleep = '{mem}' (recorded only; no suspend performed)"


def c_checkpoint_kernel_hints() -> Result:
    ptrace = Path("/proc/sys/kernel/yama/ptrace_scope")
    scope = read_text(str(ptrace)) if ptrace.exists() else "absent"
    ns_last_pid = Path("/proc/sys/kernel/ns_last_pid").exists()
    detail = f"yama ptrace_scope = {scope}; /proc/sys/kernel/ns_last_pid present = {ns_last_pid}"
    return "PASS", detail + " (inputs for Stage 02; no backend installed or run)"


def c_backend_packages() -> Result:
    if shutil.which("apt-cache") is None:
        return "SKIP", "apt-cache not available"
    proc = run(["apt-cache", "policy", "dmtcp", "criu"])
    summary = []
    current = ""
    for line in proc.stdout.splitlines():
        if line and not line.startswith(" "):
            current = line.rstrip(":")
        elif line.strip().startswith("Candidate:"):
            summary.append(f"{current} candidate {line.split(':', 1)[1].strip()}")
    if not summary:
        return "SKIP", "neither dmtcp nor criu is known to apt on this system"
    return "PASS", "; ".join(summary) + " (read-only query; nothing installed)"


def main() -> int:
    if platform.system() != "Linux":
        print(f"This script must run inside Ubuntu; this is {platform.system()}.")
        RESULTS.append(("mandatory", "FAIL", "Linux host", platform.system()))
        return summarize()

    print("### NapGuard Stage 01 Linux assumption checks")
    print(time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    if Path(".git").exists():
        run(["git", "rev-parse", "HEAD"])
        run(["git", "status", "--short"])

    with tempfile.TemporaryDirectory(prefix=".napguard-stage01-", dir=Path.home()) as raw_tmp:
        tmp = Path(raw_tmp)
        os.chmod(tmp, 0o700)
        check("mandatory", "Ubuntu platform recorded", c_platform)
        check("mandatory", "Python 3.12 with pidfd API", c_python)
        check("mandatory", "pidfd signalling and /proc start time (SEC-003, FR-009)", c_pidfd_and_start_time)
        check("mandatory", "boot_id and machine-id (CMP-005, EXC-010)", c_boot_and_machine_id)
        check("mandatory", "SO_PEERCRED on AF_UNIX (SEC-002)", c_peercred)
        check("mandatory", "XDG_RUNTIME_DIR private (SEC-001)", c_runtime_dir)
        check("mandatory", "Persistent data filesystem (PLT-007)", c_persistent_fs)
        check("mandatory", "fsync + atomic rename (REL-001, REL-002)", lambda: c_fsync_rename(tmp))
        check("mandatory", "SQLite WAL/FULL/partial index (REL-003)", lambda: c_sqlite(tmp))
        check("mandatory", "systemd user manager (FR-029)", c_user_manager)
    check("optional", "Transient user scope (OD-03)", c_transient_scope)
    check("optional", "logind InhibitDelayMaxUSec (FR-020)", c_inhibit_delay_max)
    check("optional", "logind PrepareForSleep signal (FR-020)", c_prepare_for_sleep)
    check("optional", "Unprivileged sleep delay inhibitor (FR-020)", c_delay_inhibitor)
    check("optional", "Battery exposed via power_supply (FR-017)", c_power_supply)
    check("optional", "Sleep states recorded", c_sleep_states)
    check("optional", "Checkpoint-related kernel settings", c_checkpoint_kernel_hints)
    check("optional", "Backend package availability (Stage 02 input)", c_backend_packages)
    return summarize()


def summarize() -> int:
    print("### Summary")
    for level, status, name, detail in RESULTS:
        print(f"{status:<4}  {level:<9}  {name} — {detail}")
    failed = sum(1 for level, status, _, _ in RESULTS if level == "mandatory" and status != "PASS")
    print()
    if failed:
        print(f"STAGE 01 LINUX ASSUMPTIONS: FAIL ({failed} mandatory check(s) not passed)")
        return 1
    print("STAGE 01 LINUX ASSUMPTIONS: PASS (all mandatory checks passed; review optional results)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
