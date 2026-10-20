"""Diagnostics for an early `dmtcp_restart` exit (Stage 02 py restore failure, 52e5bae).

No checkpoint or restore is run here. The tests pin down two things:

* the mechanism behind DMTCP's "Failed to restore this process as session leader"
  JWARNING: the harness starts `dmtcp_restart` in its own session, so the restarter is
  already a process-group leader and setsid() fails with EPERM for EVERY class (it was
  present for c-single and c-threads, which passed). DMTCP treats it as a warning;
* when the restarter exits, the evidence records how it ended and its unfiltered log,
  instead of only the keyword-filtered lines (which surface that warning via "strerror").
"""

from __future__ import annotations

import inspect
import signal
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from ngexp import backends, model, runner, trial
from ngexp.backends import Session
from ngexp.results import Check, Status

NONCE = "c" * 32

# The shape of DMTCP 4.2.0's JWARNING output for dmtcprestartinternal.cpp:325 (abridged).
SETSID_WARNING = [
    "[40000] WARNING at dmtcprestartinternal.cpp:325 in createProcess; "
    "REASON='JWARNING(setsid() != -1) failed'",
    "     getsid(0) = 39990",
    "     (strerror((*__errno_location ()))) = Operation not permitted",
    "Message: Failed to restore this process as session leader.",
]


def child(code: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-I", "-c", code], start_new_session=True,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def wait_exited(popen: subprocess.Popen[bytes]) -> None:
    assert runner.wait_until(lambda: runner.leader_exited_unreaped(popen), 10.0)


# ------------------------------------------------- the setsid() warning is expected


def test_harness_spawns_every_background_process_in_a_new_session() -> None:
    assert "start_new_session=True" in inspect.getsource(runner.Recorder.spawn)


def test_setsid_fails_with_eperm_in_a_process_that_already_leads_its_session() -> None:
    proc = child("import errno, os\n"
                 "try:\n    os.setsid(); print('ok')\n"
                 "except OSError as e:\n    print(errno.errorcode[e.errno])")
    out, _ = proc.communicate(timeout=10)
    assert out.decode().strip() == "EPERM"


def test_keyword_filter_surfaces_the_warning_but_can_hide_the_fatal_line(tmp_path: Path) -> None:
    err = tmp_path / "restart.err"
    err.write_text("\n".join([*SETSID_WARNING, "mtcp_restart: restore failed (SIGSEGV)"]) + "\n",
                   encoding="utf-8")
    tracked = runner.Tracked("dmtcp-restart", runner.ProcId(1, 1, 0), stderr_path=err)
    notes = backends.coordinator_log_notes(tracked)
    assert any("Operation not permitted" in n for n in notes)  # matched via "strerror"
    assert not any("SIGSEGV" in n for n in notes)  # the line that matters is filtered out
    tail = backends.log_tail(err)
    assert tail[-1] == "mtcp_restart: restore failed (SIGSEGV)" and len(tail) == 5


# ------------------------------------------------------- exit status, never reaped


def test_child_exit_status_reports_exit_code_without_reaping() -> None:
    proc = child("raise SystemExit(3)")
    wait_exited(proc)
    status = runner.child_exit_status(proc)
    assert status == {"pid": proc.pid, "how": "CLD_EXITED", "exit_code": 3}
    assert runner.leader_exited_unreaped(proc)  # still unreaped: PID/PGID still held
    assert proc.wait(timeout=5) == 3
    assert runner.child_exit_status(proc) is None  # reaped: no longer our child


def test_child_exit_status_reports_the_signal() -> None:
    proc = child("import time; time.sleep(60)")
    proc.send_signal(signal.SIGKILL)
    wait_exited(proc)
    status = runner.child_exit_status(proc)
    assert status is not None
    assert status["how"] == "CLD_KILLED" and status["signal_name"] == "SIGKILL"
    assert status["core_dumped"] is False
    proc.wait(timeout=5)


def test_child_exit_status_is_none_while_running() -> None:
    proc = child("import time; time.sleep(60)")
    try:
        assert runner.child_exit_status(proc) is None
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_restart_failure_facts_keep_unfiltered_evidence(tmp_path: Path) -> None:
    err, out, coord_err = tmp_path / "r.err", tmp_path / "r.out", tmp_path / "c.err"
    lines = [f"line {i}" for i in range(100)] + SETSID_WARNING + ["final fatal line"]
    err.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out.write_text("", encoding="utf-8")
    coord_err.write_text("dmtcp_coordinator starting...\nworker disconnected\n",
                         encoding="utf-8")
    proc = child("raise SystemExit(99)")
    wait_exited(proc)
    restarter = runner.Tracked("dmtcp-restart", runner.ProcId(proc.pid, 1, 0), proc,
                               own_group=True, stdout_path=out, stderr_path=err)
    coord = runner.Tracked("coordinator-restore", runner.ProcId(2, 1, 0), stderr_path=coord_err)
    facts = backends.restart_failure_facts(restarter, coord)
    assert facts["restarter_exit"] == {"pid": proc.pid, "how": "CLD_EXITED", "exit_code": 99}
    tail = facts["restarter_stderr_tail"]
    assert isinstance(tail, list) and len(tail) == 60 and tail[-1] == "final fatal line"
    assert facts["restore_coordinator_tail"] == ["dmtcp_coordinator starting...",
                                                 "worker disconnected"]
    assert facts["restarter_stderr_path"] == str(err)
    assert "log" in facts  # the earlier filtered field is kept for comparison
    assert runner.leader_exited_unreaped(proc)  # diagnostics reaped nothing
    proc.wait(timeout=5)


def test_restart_failure_facts_tolerate_missing_logs() -> None:
    restarter = runner.Tracked("dmtcp-restart", runner.ProcId(1, 1, 0))
    coord = runner.Tracked("coordinator-restore", runner.ProcId(2, 1, 0))
    facts = backends.restart_failure_facts(restarter, coord)
    assert facts["restarter_exit"] is None and facts["restarter_stderr_tail"] == []


# ------------------------------------- restore_and_verify on an early restarter exit


class FailingRestoreBackend(backends.Dmtcp):
    def __init__(self) -> None:
        self.tools = None
        self.version = "dmtcp_launch (DMTCP) 4.2.0"
        self.controlled_trial = True
        self.listeners = []

    def restore(self, images: Sequence[Path],
                not_before_ticks: int) -> tuple[Check, Session | None]:
        return (Check("restore command started", Status.FAIL, "dmtcp_restart exited within 1 s"),
                None)


def ngstate(counter: int) -> str:
    digest = model.digest_at(NONCE, "main", counter)
    return (f"NGSTATE v1 run=t role=main pid=7 kpid=- nonce={NONCE} counter={counter} "
            f"digest={digest:016x} ballast=- mono_ns={counter + 1}")


@pytest.mark.parametrize("restored", [[], [40, 41]])
def test_restore_failure_records_reports_written_after_termination(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restored: list[int]) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "reports").mkdir(parents=True)
    original = [ngstate(c) for c in (0, 20, 40, 450)]
    (run_dir / "reports" / "managed.txt").write_text(
        "\n".join(original + [ngstate(c) for c in restored]) + "\n", encoding="utf-8")
    state = trial.TrialState(run_dir, runner.Recorder(run_dir))
    marks: dict[str, object] = {"lower": {"main": 20}, "upper": {"main": 40},
                                "kill": {"main": 450}, "kill_line": len(original),
                                "nonce": NONCE, "original_dead": True}
    monkeypatch.setattr(trial, "boot_ticks_now", lambda: 0)
    opts = trial.Options(backend="dmtcp", workload_class="py", run_root=run_dir,
                         dmtcp_controlled_trial=True, observe_s=0.0)
    trial.restore_and_verify(state, opts, trial.CLASSES["py"], FailingRestoreBackend(),
                             runner.ArtifactSet(str(run_dir), "ext4", "/dev/vda2"), marks)
    assert [(c.name, c.status) for c in state.checks] == [
        ("restore command started", Status.FAIL)]  # nothing is judged after the failure
    late = state.facts["reports_after_kill_at_restore_fail"]
    assert late["count"] == len(restored)
    assert (late["first"]["counter"] if restored else late["first"]) == (
        restored[0] if restored else None)
