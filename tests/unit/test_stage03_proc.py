"""Stage 03a process ownership: identity, PID reuse, adoption, and cleanup (SEC-003)."""

from __future__ import annotations

import os
import platform
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from napguard.core import proc

linux_only = pytest.mark.skipif(platform.system() != "Linux", reason="needs Linux /proc")


def test_parse_stat_handles_hostile_comm() -> None:
    line = "4242 (evil) 1 2 3 4 5 6) S 1 4242 4242 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 98765 0 0"
    fields = proc.parse_stat(line)
    assert (fields.state, fields.pgid, fields.sid, fields.start_ticks) == ("S", 4242, 4242, 98765)


def test_environment_is_allow_listed() -> None:
    env = proc.filtered_env({"PATH": "/x", "HOME": "/h", "AWS_SECRET_ACCESS_KEY": "s",
                             "LD_PRELOAD": "/evil.so"}, {"EXTRA": "1"})
    assert env == {"PATH": "/x", "HOME": "/h", "EXTRA": "1"}


def test_child_exit_status_does_not_reap(tmp_path: Path) -> None:
    child = subprocess.Popen([sys.executable, "-c", "raise SystemExit(5)"])
    assert proc.wait_until(lambda: proc.leader_exited_unreaped(child), 10)
    assert proc.child_exit_status(child) == {"pid": child.pid, "how": "CLD_EXITED",
                                             "exit_code": 5}
    assert child.wait(timeout=5) == 5


def test_run_timeout_kills_the_whole_command_group(tmp_path: Path) -> None:
    tracker_dir = tmp_path / "logs"
    t = proc.ProcessTracker.__new__(proc.ProcessTracker)  # run() needs no /proc identity
    t.log_dir, t.env, t._seq = tracker_dir, proc.filtered_env(), 0
    tracker_dir.mkdir(mode=0o700)
    marker = tmp_path / "late"
    result = t.run("slow", ["/bin/sh", "-c", f"(sleep 2; touch {marker}) & sleep 30"],
                   timeout=0.5)
    assert result.timed_out and not result.ok
    proc.wait_until(lambda: marker.exists(), 3)
    assert not marker.exists()  # the background child died with the group
    assert oct((tracker_dir / "001-slow.err").stat().st_mode & 0o777) == "0o600"


@linux_only
def test_forged_identity_and_reused_pid_are_never_signalled() -> None:
    child = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        info = proc.read_info(child.pid)
        assert info is not None
        reused = proc.ProcId(child.pid, info.ident.start_ticks + 1, info.ident.uid)
        assert proc.signal_identity(reused, signal.SIGKILL) is False
        assert proc.terminate_identity(reused, grace=0.2) is True  # "gone": it is not ours
        assert child.poll() is None  # the real process was not touched
    finally:
        child.kill()
        child.wait()


@linux_only
def test_group_termination_kills_orphaned_members(tmp_path: Path) -> None:
    t = proc.ProcessTracker(tmp_path / "logs")
    leader = t.spawn("tree", ["/bin/sh", "-c", "sleep 60 & sleep 60 & wait"])
    proc.wait_until(lambda: len(proc.running_group_members(leader.ident.pid)) >= 3, 5)
    assert t.cleanup() == []
    assert proc.running_group_members(leader.ident.pid) == []


@linux_only
def test_adoption_requires_our_session_and_a_late_start(tmp_path: Path) -> None:
    t = proc.ProcessTracker(tmp_path / "logs")
    foreign = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        assert t.adopt("foreign", foreign.pid) is None  # not a session this tracker created
        ours = t.spawn("ours", ["sleep", "30"])
        assert t.adopt("again", ours.ident.pid) is ours
        assert t.adopt("late", ours.ident.pid, not_before_ticks=ours.ident.start_ticks + 10**9) \
            is None
    finally:
        foreign.kill()
        foreign.wait()
        t.cleanup()


@linux_only
def test_crash_cleanup_stops_only_identity_matched_processes(tmp_path: Path) -> None:
    t = proc.ProcessTracker(tmp_path / "logs")
    ours = t.spawn("ours", ["sleep", "30"])
    bystander = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        info = proc.read_info(bystander.pid)
        assert info is not None
        records = t.records() + [
            {"name": "reused", "pid": bystander.pid, "start_ticks": info.ident.start_ticks + 1,
             "uid": os.getuid(), "boot_id": proc.boot_id()},
            {"name": "old-boot", "pid": bystander.pid, "start_ticks": info.ident.start_ticks,
             "uid": os.getuid(), "boot_id": "another-boot"},
            {"name": "broken", "pid": "x"}]
        state = proc.SessionState(str(tmp_path), 4100, records)
        state.write(tmp_path / "session.json")
        stopped, skipped = proc.cleanup_recorded(proc.SessionState.read(tmp_path / "session.json"))
        assert stopped == [f"ours(pid {ours.ident.pid})"]
        assert len(skipped) == 3 and bystander.poll() is None  # bystander untouched
    finally:
        bystander.kill()
        bystander.wait()
        t.cleanup()


@linux_only
def test_leak_scan_reports_but_does_not_kill(tmp_path: Path) -> None:
    marker = tmp_path / "session"
    marker.mkdir()
    child = subprocess.Popen(["sleep", "30", str(marker)], start_new_session=True)
    try:
        assert [p.ident.pid for p in proc.leaked_processes(marker)] == [child.pid]
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()
