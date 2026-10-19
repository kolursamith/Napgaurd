"""Stage 02 safety boundaries: no insecure coordinator, no foreign process is touched."""

from __future__ import annotations

import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ngexp import backends, runner
from ngexp.results import Check, Status

linux_only = pytest.mark.skipif(platform.system() != "Linux", reason="needs Linux /proc")


def test_parse_stat_handles_hostile_comm() -> None:
    # comm may contain spaces and ')' — fields must be counted from the LAST ')'.
    fields = ["S", "1", "4242", "4242"] + ["0"] * 15 + ["987654"] + ["0"] * 10
    text = "4242 (evil) (name)) " + " ".join(fields)
    parsed = runner.parse_stat(text)
    assert parsed == runner.StatFields(state="S", pgid=4242, sid=4242, start_ticks=987654)


SS_TCP = (
    'LISTEN 0 128 127.0.0.1:45678 0.0.0.0:* users:(("dmtcp_coordinat",pid=500,fd=821),'
    '("python3",pid=400,fd=3))\n'
    'LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n'
    'LISTEN 0 4096 [::]:41000 [::]:* users:(("ngcounter",pid=600,fd=5))\n'
    'LISTEN 0 128 [::1]:631 [::]:*\n'
)


def test_parse_ss_listeners_and_violations() -> None:
    listeners = runner.parse_ss_listeners(SS_TCP)
    assert [(lst.local, lst.loopback, lst.pids) for lst in listeners] == [
        ("127.0.0.1:45678", True, (500, 400)),
        ("0.0.0.0:22", False, ()),
        ("[::]:41000", False, (600,)),
        ("[::1]:631", True, ()),
    ]
    assert listeners[0].port == 45678
    # Only experiment-owned non-loopback listeners count: sshd on :22 (no visible owner)
    # is not ours; the workload's [::]:41000 is a violation.
    assert runner.listener_violations(listeners, {500, 600}) == [listeners[2]]
    assert runner.listener_violations(listeners, {500}) == []


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


GOOD_COORDINATOR = """
import socket, sys, time
args = sys.argv[1:]
listener = socket.socket(fileno=821)          # inherited, pre-bound by the harness
with open(args[args.index("--port-file") + 1], "w") as fh:
    fh.write(str(listener.getsockname()[1]))
time.sleep(60)
"""

# Simulates a coordinator that ignores the inherited listener and falls back to
# INADDR_ANY on --coord-port, as DMTCP would if the hand-off stopped working.
FALLBACK_COORDINATOR = """
import socket, sys, time
args = sys.argv[1:]
port = int(args[args.index("--coord-port") + 1])
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", port))                    # must fail: EADDRINUSE
s.listen(1)
with open(args[args.index("--port-file") + 1], "w") as fh:
    fh.write(str(port))
time.sleep(60)
"""


def coordinator_backend(tmp_path: Path, source: str) -> backends.Dmtcp:
    script = tmp_path / "fake_coordinator.py"
    script.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
    script.chmod(0o700)
    dmtcp = fake_dmtcp(tmp_path)
    dmtcp.tools = backends.DmtcpTools(str(script), str(script), str(script), str(script))
    dmtcp.version = "dmtcp_launch (DMTCP) 4.2.0"
    return dmtcp


@linux_only
def test_wildcard_bind_is_refused_next_to_loopback_listener() -> None:
    listener = runner.loopback_listener()
    try:
        assert backends.wildcard_bind_refused(listener.getsockname()[1]) is True
    finally:
        listener.close()


@linux_only
def test_loopback_coordinator_handoff_is_confined(tmp_path: Path) -> None:
    dmtcp = coordinator_backend(tmp_path, GOOD_COORDINATOR)
    coord, port, facts = dmtcp._loopback_coordinator("fake-coordinator")
    assert facts["port"] == port and not any(facts["non_loopback_connect_results"].values())
    try:
        listeners = [lst for lst in runner.current_listeners() if lst.port == port]
        assert listeners and all(lst.loopback for lst in listeners)
        assert coord.ident.pid in {pid for lst in listeners for pid in lst.pids}
    finally:
        assert dmtcp.rec.terminate(coord)
        dmtcp.cleanup([])


@linux_only
def test_coordinator_ignoring_handoff_fails_closed(tmp_path: Path) -> None:
    dmtcp = coordinator_backend(tmp_path, FALLBACK_COORDINATOR)
    with pytest.raises(backends.BackendStepError, match="did not adopt the loopback listener"):
        dmtcp._loopback_coordinator("fallback-coordinator")
    dmtcp.cleanup([])
    assert dmtcp.rec.cleanup() == []
    exposed = [lst for lst in runner.current_listeners() if not lst.loopback
               and set(lst.pids) & {t.ident.pid for t in dmtcp.rec.tracked}]
    assert exposed == []


def test_trial_gate_stays_closed_even_with_handoff_available(tmp_path: Path) -> None:
    dmtcp = coordinator_backend(tmp_path, GOOD_COORDINATOR)
    with pytest.raises(backends.BackendStepError, match="refusing to start a DMTCP coordinator"):
        dmtcp.launch(["/bin/sleep", "30"])
    assert dmtcp.rec.tracked == []


def test_listener_audit_failure_kills_coordinator_and_fails(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dmtcp = fake_dmtcp(tmp_path)
    killed: list[int] = []

    def no_ss() -> list[runner.Listener]:
        raise RuntimeError("ss: command not found")

    monkeypatch.setattr(backends, "current_listeners", no_ss)
    def record_kill(tracked: runner.Tracked, grace: float = 3.0,
                    sig: int = signal.SIGTERM) -> bool:
        killed.append(sig)
        return True

    monkeypatch.setattr(dmtcp.rec, "terminate", record_kill)
    coord = runner.Tracked("fake", runner.ProcId(999_999, 1, os.getuid()))
    with pytest.raises(backends.BackendStepError, match="listener audit failed"):
        dmtcp.verify_isolation(coord, 45678, "fake")
    assert killed == [signal.SIGKILL]


def test_isolation_check_reports_fail_instead_of_crashing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dmtcp = fake_dmtcp(tmp_path)
    monkeypatch.setattr(dmtcp, "probe", lambda: Check(
        "backend available", Status.BLOCKED, "DMTCP 4.2.0 installed, but refusing", facts={}))

    def broken(label: str) -> tuple[runner.Tracked, int, dict[str, object]]:
        raise backends.BackendStepError("could not start isolation-coordinator: fd 821 in use")

    monkeypatch.setattr(dmtcp, "_loopback_coordinator", broken)
    checks = dmtcp.isolation_check()
    assert [c.status for c in checks] == [Status.PASS, Status.FAIL]
    assert "fd 821 in use" in checks[1].detail


def test_missing_ip_tool_is_an_error_not_an_empty_address_list(
        monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["ip"], 1, "", "ip: not found")

    monkeypatch.setattr(subprocess, "run", failing_run)  # runner calls subprocess.run
    with pytest.raises(RuntimeError, match="ip -o -4 addr show failed"):
        runner.non_loopback_ipv4_addresses()


def fake_dmtcp_prefix(tmp_path: Path) -> Path:
    prefix = tmp_path / "dmtcp-4.2.0"
    (prefix / "bin").mkdir(parents=True)
    for name in ("dmtcp_launch", "dmtcp_command", "dmtcp_coordinator", "dmtcp_restart"):
        tool = prefix / "bin" / name
        tool.write_text("#!/bin/sh\necho 'dmtcp_launch (DMTCP) 4.2.0'\n", encoding="utf-8")
        tool.chmod(0o700)
    return prefix


@linux_only
@pytest.mark.parametrize("entry", ["trial", "matrix", "reboot-prepare"])
def test_dmtcp_trial_matrix_and_reboot_gates_stay_closed(tmp_path: Path, entry: str) -> None:
    from ngexp import trial

    opts = trial.Options(backend="dmtcp", workload_class="c-single", run_root=tmp_path / "runs",
                         dmtcp_prefix=fake_dmtcp_prefix(tmp_path))
    opts.run_root.mkdir()
    if entry == "trial":
        status = trial.run_trial(opts)
    elif entry == "matrix":
        status = trial.run_matrix(opts, ["c-single", "py"])
    else:
        status = trial.reboot_prepare(opts)
    assert status is Status.BLOCKED
    for commands in opts.run_root.glob("*/commands.jsonl"):
        assert '"kind": "spawn"' not in commands.read_text(encoding="utf-8")
    assert not list(opts.run_root.glob("*/reboot-manifest.json"))


BUILD_DMTCP = Path(__file__).resolve().parents[2] / "experiments" / "stage02" / "build-dmtcp.sh"


def run_build_script(tmp_path: Path, jobs: str | None, *args: str) -> subprocess.CompletedProcess[str]:
    # Only invalid settings or --check-config are used: both exit before any clone or build.
    env = {k: v for k, v in os.environ.items() if not k.startswith("NAPGUARD_STAGE02_")}
    env.update(NAPGUARD_STAGE02_SRC=str(tmp_path / "src"),
               NAPGUARD_STAGE02_PREFIX=str(tmp_path / "prefix"))
    if jobs is not None:
        env["NAPGUARD_STAGE02_BUILD_JOBS"] = jobs
    return subprocess.run(["bash", str(BUILD_DMTCP), *args], env=env, capture_output=True,
                          text=True, timeout=30)


@pytest.mark.parametrize("jobs", ["0", "-1", "abc", "1.5", "2 ", " 2", "02", "2;id", "$(id)"])
def test_build_jobs_rejects_invalid_values_before_doing_anything(tmp_path: Path, jobs: str) -> None:
    done = run_build_script(tmp_path, jobs)
    assert done.returncode == 2
    assert "must be a positive integer" in done.stderr
    assert done.stderr.rstrip().endswith("'")  # the exit code is not echoed into the message
    assert not (tmp_path / "src").exists() and not (tmp_path / "prefix").exists()


@pytest.mark.parametrize(("jobs", "expected"), [(None, "jobs=2"), ("1", "jobs=1"), ("6", "jobs=6")])
def test_build_jobs_default_and_valid_values(tmp_path: Path, jobs: str | None,
                                             expected: str) -> None:
    done = run_build_script(tmp_path, jobs, "--check-config")
    assert done.returncode == 0, done.stderr
    assert expected in done.stdout.split()
    assert "commit=f8009ce7b4ad211311ca2f72a929b975e4aa1155" in done.stdout
    assert not (tmp_path / "src").exists() and not (tmp_path / "prefix").exists()


def test_build_script_uses_bounded_jobs_and_rejects_unknown_arguments(tmp_path: Path) -> None:
    text = BUILD_DMTCP.read_text(encoding="utf-8")
    assert 'make -j"$JOBS"' in text and 'make -j"$(nproc)"' not in text
    done = run_build_script(tmp_path, None, "--build-now")
    assert done.returncode == 2 and "unknown argument" in done.stderr
