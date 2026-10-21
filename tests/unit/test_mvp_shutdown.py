"""MVP daemon shutdown (Ubuntu 43832c3: exit 143, daemon and socket left behind).

Root cause: the Ubuntu script backgrounded a shell FUNCTION, so `$!` was a wrapper
subshell; SIGTERM killed the wrapper and never reached the daemon. These tests pin the
fixed start/stop helpers (scripts/lib/daemon-control.sh) with a fake daemon, and the real
daemon's SIGTERM handling in a subprocess.
"""

from __future__ import annotations

import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts" / "lib" / "daemon-control.sh"
SCRIPT = ROOT / "scripts" / "mvp-ubuntu-check.sh"

FAKE_DAEMON = """
import os, signal, sys, time
sock, mode = sys.argv[1], sys.argv[2]
open(sock, "w").close()
def on_term(*_):
    if mode == "ignore":
        return
    if mode != "leave-socket":
        os.unlink(sock)
    sys.exit(1 if mode == "exit-1" else 0)
signal.signal(signal.SIGTERM, on_term)
while True:
    time.sleep(0.05)
"""


@pytest.fixture
def short_dir() -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(prefix="ng-", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def drive(tmp: Path, mode: str, pattern: str = "fake-napguard-daemon") -> dict[str, str]:
    fake = tmp / "fake-napguard-daemon.py"
    fake.write_text(FAKE_DAEMON, encoding="utf-8")
    sock = tmp / "daemon.sock"
    script = f"""
. "{LIB}"
start_daemon "{tmp}/daemon.log" "{sys.executable}" -I "{fake}" "{sock}" {mode}
for _ in $(seq 1 100); do [ -e "{sock}" ] && break; sleep 0.05; done
echo "PID=$DAEMON_PID"
echo "ARGS=$(ps -o args= -p "$DAEMON_PID")"
stop_daemon "{sock}" 3 "{pattern}"
echo "RC=$?"
echo "DAEMON_RC=$DAEMON_RC"
echo "SOCKET=$([ -e "{sock}" ] && echo left || echo removed)"
"""
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    out = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
    out["stderr"] = done.stderr
    return out


def gone(pid: str) -> bool:
    return subprocess.run(["ps", "-p", pid], capture_output=True).returncode != 0


def test_daemon_pid_is_the_daemon_and_a_clean_stop_passes(short_dir: Path) -> None:
    out = drive(short_dir, "clean")
    assert "fake-napguard-daemon" in out["ARGS"]  # $! is the daemon, not a wrapper shell
    assert (out["RC"], out["DAEMON_RC"], out["SOCKET"]) == ("0", "0", "removed")
    assert gone(out["PID"])


@pytest.mark.parametrize(("mode", "daemon_rc", "socket"), [
    ("leave-socket", "0", "left"),  # exited, but its socket was left behind
    ("exit-1", "1", "removed"),  # exited with a failure status
])
def test_unclean_stops_are_failures(short_dir: Path, mode: str, daemon_rc: str,
                                    socket: str) -> None:
    out = drive(short_dir, mode)
    assert (out["RC"], out["DAEMON_RC"], out["SOCKET"]) == ("1", daemon_rc, socket)
    assert gone(out["PID"])


def test_a_daemon_that_ignores_sigterm_times_out(short_dir: Path) -> None:
    out = drive(short_dir, "ignore")
    try:
        assert (out["RC"], out["DAEMON_RC"]) == ("1", "timeout")
        assert not gone(out["PID"])  # reported, still running: never claimed stopped
    finally:
        os.kill(int(out["PID"]), signal.SIGKILL)


def test_a_process_that_is_not_ours_is_never_signalled(short_dir: Path) -> None:
    out = drive(short_dir, "clean", pattern="napguard.main")  # wrong identity
    try:
        assert (out["RC"], out["DAEMON_RC"]) == ("1", "not-ours")
        assert not gone(out["PID"])
    finally:
        os.kill(int(out["PID"]), signal.SIGTERM)


def test_script_starts_the_daemon_directly_and_waits_for_cleanup() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "ng daemon" not in code  # the backgrounded-function wrapper is gone
    assert 'start_daemon "$OUT/daemon.log" env PYTHONPATH="$REPO/src" python3 -s -m napguard.main' \
        in code
    assert 'stop_daemon "$SOCK" 60 napguard.main' in code
    assert "kill -TERM" not in code  # only through stop_daemon


DAEMON = """
import sys
from pathlib import Path
from mvp_fakes import FakeBackend, fake_build
from napguard.daemon.service import Service
from napguard.power.battery import BatteryMonitor
base = Path(sys.argv[1])
service = Service(base / "data", base / "run" / "daemon.sock", FakeBackend(),
                  coordinator_acknowledged=True, build=fake_build, require_persistent=False,
                  battery=BatteryMonitor(base / "none"), peer=lambda s: __import__("os").getuid(),
                  manager_options={"verify_window_s": 0.2, "report_timeout_s": 10})
sys.exit(service.run_until_signal())
"""


def test_real_daemon_exits_cleanly_on_sigterm_and_stops_its_workloads(short_dir: Path) -> None:
    env = dict(os.environ, PYTHONPATH=f"{ROOT / 'src'}{os.pathsep}{ROOT / 'tests' / 'unit'}")
    proc = subprocess.Popen([sys.executable, "-c", DAEMON, str(short_dir)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    sock = short_dir / "run" / "daemon.sock"
    try:
        deadline = time.monotonic() + 20
        while not sock.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert sock.exists(), "daemon did not start"
        sys.path.insert(0, str(ROOT / "src"))
        from napguard.daemon.ipc import Client
        from napguard.daemon.service import wait_operation

        client = Client(sock, timeout=10)
        accepted = client.call("workloads.launch_demo", workload_class="c-threads")
        assert wait_operation(client.call, accepted["operation_id"], 30, 0.05)["state"] == \
            "SUCCEEDED"
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    output = proc.stdout.read().decode() if proc.stdout else ""
    assert rc == 0, output
    assert not sock.exists()
    assert "napguard daemon stopped" in output
    db = sqlite3.connect(short_dir / "data" / "catalog.db")
    states = [r[0] for r in db.execute("SELECT state FROM instances")]
    db.close()
    assert states == ["STOPPED"]  # the managed workload was stopped by the shutdown
