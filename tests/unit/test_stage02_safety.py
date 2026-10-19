"""Stage 02 safety boundaries: no insecure coordinator, no foreign process is touched."""

from __future__ import annotations

import os
import platform
import signal
import subprocess
import time
from pathlib import Path

import pytest

from ngexp import backends, runner
from ngexp.results import Status

linux_only = pytest.mark.skipif(platform.system() != "Linux", reason="needs Linux /proc")


def test_parse_stat_handles_hostile_comm() -> None:
    # comm may contain spaces and ')' — fields must be counted from the LAST ')'.
    fields = ["S", "1", "4242", "4242"] + ["0"] * 15 + ["987654"] + ["0"] * 10
    text = "4242 (evil) (name)) " + " ".join(fields)
    parsed = runner.parse_stat(text)
    assert parsed == runner.StatFields(state="S", pgid=4242, sid=4242, start_ticks=987654)


@pytest.mark.parametrize(
    ("ss_output", "expected"),
    [
        ("LISTEN 0 128 127.0.0.1:7779 0.0.0.0:*\n", True),
        ("LISTEN 0 128 [::1]:7779 [::]:*\n", True),
        ("LISTEN 0 128 0.0.0.0:7779 0.0.0.0:*\n", False),
        ("LISTEN 0 128 127.0.0.1:7779 0.0.0.0:*\nLISTEN 0 128 [::]:7779 [::]:*\n", False),
        ("LISTEN 0 128 192.168.64.2:7779 0.0.0.0:*\n", False),
        ("", False),  # no evidence of a listener is not evidence of safety
    ],
)
def test_listeners_loopback_only(ss_output: str, expected: bool) -> None:
    assert backends.listeners_loopback_only(ss_output) is expected


def fake_dmtcp(tmp_path: Path) -> backends.Dmtcp:
    rec = runner.Recorder(tmp_path / "run")
    dmtcp = backends.Dmtcp(rec, None)
    true = "/usr/bin/true" if Path("/usr/bin/true").exists() else "/bin/true"
    dmtcp.tools = backends.DmtcpTools(true, true, true, true)
    return dmtcp


def test_dmtcp_probe_is_blocked_until_coordinator_can_be_restricted(tmp_path: Path) -> None:
    assert backends.DMTCP_LOOPBACK_ONLY_COORDINATOR_VERIFIED is False
    check = fake_dmtcp(tmp_path).probe()
    assert check.status is Status.BLOCKED
    assert "JSockAddr::ANY" in check.detail and "OD-13" in check.detail


def test_dmtcp_refuses_to_spawn_a_coordinator(tmp_path: Path) -> None:
    dmtcp = fake_dmtcp(tmp_path)
    with pytest.raises(backends.BackendStepError, match="refusing to start a DMTCP coordinator"):
        dmtcp.launch(["/bin/sleep", "30"])
    with pytest.raises(backends.BackendStepError):
        dmtcp.restore([tmp_path / "ckpt_x.dmtcp"], not_before_ticks=0)
    assert dmtcp.rec.tracked == []  # nothing was started
    commands = tmp_path / "run" / "commands.jsonl"
    assert not commands.exists() or "spawn" not in commands.read_text(encoding="utf-8")


def test_build_script_refuses_dmtcp_make_check() -> None:
    script = Path(__file__).resolve().parents[2] / "experiments" / "stage02" / "build-dmtcp.sh"
    text = script.read_text(encoding="utf-8")
    assert "RUN_MAKE_CHECK is refused" in text
    assert "make check 2>&1" not in text


@linux_only
def test_group_termination_also_kills_orphaned_members(tmp_path: Path) -> None:
    rec = runner.Recorder(tmp_path / "run")
    # The leader exits immediately, leaving a background member in the harness's own group.
    leader = rec.spawn("group", ["sh", "-c", "sleep 30 & echo $! > member.pid; exit 0"],
                       cwd=rec.run_dir)
    assert runner.wait_until(lambda: (rec.run_dir / "member.pid").exists(), timeout=5)
    time.sleep(0.2)
    member = int((rec.run_dir / "member.pid").read_text().strip())
    assert runner.read_info(member) is not None
    assert rec.terminate(leader) is True
    assert runner.read_info(member) is None or not runner.read_info(member).running  # type: ignore[union-attr]


@linux_only
def test_adopt_refuses_processes_outside_the_run(tmp_path: Path) -> None:
    rec = runner.Recorder(tmp_path / "run")
    # This test process and its parent are the user's own processes but were not
    # created by the run: they must never be adopted (and so never signalled).
    assert rec.adopt("self", os.getpid()) is None
    assert rec.adopt("parent", os.getppid()) is None
    outsider = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        assert rec.adopt("outsider", outsider.pid) is None
        # Even inside a run session, a process started before the cut-off is refused.
        rec.sessions.add(outsider.pid)
        assert rec.adopt("too-early", outsider.pid, not_before_ticks=runner.boot_ticks_now() + 100) is None
        assert rec.cleanup() == []
        assert outsider.poll() is None  # untouched by cleanup
    finally:
        outsider.send_signal(signal.SIGKILL)
        outsider.wait()


@linux_only
def test_signal_identity_ignores_a_different_process(tmp_path: Path) -> None:
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        info = runner.read_info(proc.pid)
        assert info is not None
        forged = runner.ProcId(proc.pid, info.ident.start_ticks + 1, info.ident.uid)
        assert runner.signal_identity(forged, signal.SIGKILL) is False
        assert proc.poll() is None  # the wrong identity was not signalled
    finally:
        proc.kill()
        proc.wait()
