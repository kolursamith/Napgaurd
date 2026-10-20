"""Stage 02 safety boundaries: no insecure coordinator, no foreign process is touched."""

from __future__ import annotations

import os
import platform
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ngexp import backends, cli, runner, trial
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


# ------------------------------------------------- ADR 0004 controlled-trial exception


def controlled_opts(tmp_path: Path, **overrides: object) -> trial.Options:
    values: dict[str, object] = {"backend": "dmtcp", "workload_class": "c-single",
                                 "run_root": tmp_path, "dmtcp_controlled_trial": True}
    values.update(overrides)
    return trial.Options(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "entry", "reason"),
    [
        ({}, "matrix", "not `matrix`"),
        ({}, "dmtcp-isolation-check", "not `dmtcp-isolation-check`"),
        ({"backend": "criu"}, "trial", "DMTCP backend only"),
        ({"workload_class": "c-threads"}, "reboot-prepare", "not allowed under ADR 0004"),
        ({"workload_class": "py"}, "reboot-resume", "not allowed under ADR 0004"),
        ({"workload_class": "c-child"}, "trial", "not allowed under ADR 0004 at all"),
        ({"workload_class": "tcp"}, "trial", "not allowed under ADR 0004 at all"),
        ({"workload_class": "gui"}, "trial", "not allowed under ADR 0004 at all"),
    ],
)
def test_controlled_trial_exception_is_narrow(tmp_path: Path, overrides: dict[str, object],
                                              entry: str, reason: str) -> None:
    refusal = trial.controlled_trial_refusal(controlled_opts(tmp_path, **overrides), entry)
    assert refusal is not None and reason in refusal


def test_controlled_trial_exception_allows_only_dmtcp_c_single_trial(tmp_path: Path) -> None:
    for entry in ("trial", "reboot-prepare", "reboot-resume"):
        assert trial.controlled_trial_refusal(controlled_opts(tmp_path), entry) is None
    off = controlled_opts(tmp_path, dmtcp_controlled_trial=False)
    assert trial.controlled_trial_refusal(off, "matrix") is None  # not requested: normal gates
    assert trial.Options(backend="dmtcp", workload_class="c-single",
                         run_root=tmp_path).dmtcp_controlled_trial is False


def test_controlled_trial_flag_exists_only_on_trial_and_reboot_commands() -> None:
    for argv in (["trial", "--backend", "dmtcp"], ["reboot-prepare", "--backend", "dmtcp"],
                 ["reboot-resume", "--backend", "dmtcp", "--class", "c-single"]):
        assert cli.parser().parse_args([*argv, "--adr-0004-controlled-trial"]).dmtcp_controlled_trial
        assert cli.parser().parse_args(argv).dmtcp_controlled_trial is False
    for other in (["matrix", "--backend", "dmtcp"], ["dmtcp-isolation-check"], ["env"]):
        with pytest.raises(SystemExit):
            cli.parser().parse_args([*other, "--adr-0004-controlled-trial"])


def test_global_gate_unchanged_and_exception_is_per_instance(tmp_path: Path) -> None:
    assert backends.DMTCP_LOOPBACK_ONLY_COORDINATOR_VERIFIED is False
    assert fake_dmtcp(tmp_path).probe().status is Status.BLOCKED
    allowed = fake_dmtcp(tmp_path / "x")
    tools = fake_dmtcp_prefix(tmp_path / "tools") / "bin"
    allowed.tools = backends.DmtcpTools(*(str(tools / n) for n in (
        "dmtcp_launch", "dmtcp_command", "dmtcp_coordinator", "dmtcp_restart")))
    allowed.controlled_trial = True
    assert allowed.probe().status is Status.PASS  # 4.2.0 reported by the fake tools
    assert fake_dmtcp(tmp_path / "y").controlled_trial is False


def test_controlled_trial_still_requires_the_analysed_version(tmp_path: Path) -> None:
    dmtcp = fake_dmtcp(tmp_path)
    dmtcp.controlled_trial = True
    dmtcp.version = "dmtcp_launch (DMTCP) 4.3.0"
    with pytest.raises(backends.BackendStepError, match="analysed for DMTCP 4.2.0"):
        dmtcp.launch(["/bin/sleep", "30"])
    assert dmtcp.rec.tracked == []


@linux_only
def test_workload_socket_detection_ignores_only_dmtcp_protected_fds(tmp_path: Path) -> None:
    child_src = (
        "import os, socket, sys, time\n"
        "s = socket.socket()\n"
        "if sys.argv[1] == 'protected':\n"
        "    os.dup2(s.fileno(), 821); s.close()\n"
        "print('ready', flush=True); time.sleep(30)\n"
    )
    for mode, expect_sockets in (("protected", False), ("workload", True)):
        proc = subprocess.Popen([sys.executable, "-c", child_src, mode], stdout=subprocess.PIPE,
                                text=True, start_new_session=True)
        try:
            assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
            assert any(fd == 821 for fd, _ in runner.socket_fds(proc.pid)) is (mode == "protected")
            assert bool(backends.workload_socket_fds(proc.pid)) is expect_sockets
        finally:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()



def test_controlled_probe_refuses_unanalysed_dmtcp_version(tmp_path: Path) -> None:
    dmtcp = fake_dmtcp(tmp_path)  # /usr/bin/true: reports no version
    dmtcp.controlled_trial = True
    check = dmtcp.probe()
    assert check.status is Status.BLOCKED and "covers DMTCP 4.2.0 only" in check.detail


def test_new_non_loopback_listener_is_detected_regardless_of_owner() -> None:
    before = runner.parse_ss_listeners("LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n")
    baseline = {(lst.proto, lst.local) for lst in before if not lst.loopback}
    after = runner.parse_ss_listeners(
        "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n"
        "LISTEN 0 128 127.0.0.1:45678 0.0.0.0:*\n"
        "LISTEN 0 128 0.0.0.0:41000 0.0.0.0:*\n")  # no visible owner
    assert [lst.local for lst in runner.new_non_loopback(after, baseline)] == ["0.0.0.0:41000"]


def guard_state(tmp_path: Path, baseline: set[tuple[str, str]] | None) -> trial.TrialState:
    state = trial.TrialState(tmp_path, runner.Recorder(tmp_path))
    state.baseline_listeners = baseline
    return state


def test_guard_refuses_without_a_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trial, "current_listeners", lambda: [])
    with pytest.raises(backends.BackendStepError, match="no listener baseline"):
        trial.guard_network(guard_state(tmp_path, None), "launch")


def test_guard_aborts_on_new_unowned_wildcard_listener(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    listeners = runner.parse_ss_listeners("LISTEN 0 128 [::]:41000 [::]:*\n")
    monkeypatch.setattr(trial, "current_listeners", lambda: listeners)
    with pytest.raises(backends.BackendStepError, match=r"listening beyond loopback.*\[::\]:41000"):
        trial.guard_network(guard_state(tmp_path, set()), "restore (waiting)")
    # A listener that already existed before the trial (e.g. sshd) does not abort it.
    trial.guard_network(guard_state(tmp_path, {("tcp", "[::]:41000")}), "restore (waiting)")


@linux_only
def test_leak_scan_finds_untracked_processes_of_the_run(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "bin").mkdir(parents=True)
    sleeper = run_dir / "bin" / "sleeper"
    sleeper.write_bytes(Path("/bin/sleep").read_bytes())
    sleeper.chmod(0o700)
    proc = subprocess.Popen([str(sleeper), "30"], start_new_session=True)
    try:
        assert runner.wait_until(lambda: any(p.ident.pid == proc.pid
                                             for p in runner.run_dir_processes(run_dir)), 5)
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    assert not any(p.ident.pid == proc.pid for p in runner.run_dir_processes(run_dir))


CONTROLLED_SCRIPT = Path(__file__).resolve().parents[2] / "experiments" / "stage02" / \
    "ubuntu-controlled-trial.sh"


@pytest.mark.parametrize("args", [[], ["abc"], ["A" * 40], ["a" * 40, "--go"],
                                  ["a" * 40, "--run-trial", "extra"]])
def test_controlled_trial_script_rejects_bad_arguments(args: list[str]) -> None:
    done = subprocess.run(["bash", str(CONTROLLED_SCRIPT), *args], capture_output=True,
                          text=True, timeout=30)
    assert done.returncode == 2 and "usage:" in done.stderr


def test_controlled_trial_script_runs_trial_only_when_explicitly_requested() -> None:
    text = CONTROLLED_SCRIPT.read_text(encoding="utf-8")
    head, rest = text.split('case "$MODE" in\n  trial)', 1)  # the step-6 mode block
    gated = rest.split("\nesac\n", 1)[0]
    invocations = [ln for ln in text.splitlines() if "run_stage02.py" in ln
                   and ("trial --backend" in ln or "reboot-" in ln)]
    assert len(invocations) == 3  # trial, reboot-prepare, reboot-resume
    assert all(ln in gated for ln in invocations)  # each only inside the explicit-mode block
    assert gated.count("--adr-0004-controlled-trial") == 3
    assert "*)" in gated and "Nothing run" in gated  # no mode flag: nothing is run
    # Every prerequisite before the mode block is a hard stop (BLOCKED).
    assert head.count("|| blocked") >= 5
    # The script never reboots, powers off, or escalates privileges.
    command_word = re.compile(r"(^|[;&|(]\s*)(sudo|systemctl|reboot|poweroff|shutdown|halt)(\s|$)")
    for line in text.splitlines():
        if not line.lstrip().startswith("#"):
            assert not command_word.search(line.strip()), line
