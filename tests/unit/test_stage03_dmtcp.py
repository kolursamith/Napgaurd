"""Stage 03a DMTCP adapter: availability, fail-closed refusals, and cleanup.

Portable tests use fake tool scripts and never start a coordinator. Linux-only tests
start FAKE coordinators (small Python scripts) through the real fail-closed hand-off and
the real `ss`/`ip` audits. No test runs real DMTCP or checkpoints anything.
"""

from __future__ import annotations

import platform
import struct
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest

from napguard.checkpoint import artifacts, dmtcp
from napguard.checkpoint.base import (
    ArtifactRole,
    BackendError,
    CaptureResult,
    CheckpointOptions,
    ErrorCode,
    RestoreOptions,
    SessionState,
    WorkloadSpec,
)
from napguard.checkpoint.dmtcp_completion import wait_for_checkpoint
from napguard.checkpoint.policy import CoordinatorPolicy
from napguard.core import netaudit, proc

linux_only = pytest.mark.skipif(platform.system() != "Linux", reason="needs Linux /proc and ss")
VERIFIED = {"arch": "aarch64", "kernel_release": "6.8.0", "os_id": "ubuntu",
            "os_version_id": "24.04"}
DEV = CoordinatorPolicy.experimental_single_user(True)


def elf_bytes(machine: int = 183) -> bytes:
    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4], header[5], header[6] = 2, 1, 1
    struct.pack_into("<HHI", header, 16, 3, machine, 1)
    struct.pack_into("<Q", header, 32, 64)
    struct.pack_into("<HH", header, 54, 56, 1)
    ph = bytearray(56)
    struct.pack_into("<I", ph, 0, 3)  # PT_INTERP: dynamically linked
    return bytes(header + ph)


def make_tools(tmp_path: Path, version: str = "4.2.0", launch: str | None = None,
               coordinator: str | None = None, command: str | None = None,
               restart: str | None = None, commit: str = dmtcp.PINNED_COMMIT
               ) -> dmtcp.DmtcpTools:
    bindir = tmp_path / "dmtcp" / "bin"
    bindir.mkdir(parents=True)
    head = f'#!/bin/sh\nif [ "$1" = "--version" ]; then echo "dmtcp_launch (DMTCP) {version}"; ' \
           'exit 0; fi\n'
    bodies = {"dmtcp_launch": launch or "exit 0\n", "dmtcp_command": command or "exit 0\n",
              "dmtcp_coordinator": coordinator, "dmtcp_restart": restart or "exit 0\n"}
    for name, body in bodies.items():
        tool = bindir / name
        if body is not None and body.startswith("#!"):
            tool.write_text(body, encoding="utf-8")  # a complete script (fake coordinator)
        else:
            tool.write_text(head + (body or "sleep 60\n"), encoding="utf-8")
        tool.chmod(0o700)
    (tmp_path / "dmtcp" / dmtcp.BUILD_INFO).write_text(
        f"version={version}\ncommit={commit}\n", encoding="utf-8")
    found = dmtcp.find_tools(tmp_path / "dmtcp")
    assert found is not None
    return found


def backend(tmp_path: Path, policy: CoordinatorPolicy | None = None, version: str = "4.2.0",
            host: Any = lambda uid: [], platform_facts: dict[str, str] | None = None,
            **tools: str) -> dmtcp.DmtcpBackend:
    return dmtcp.DmtcpBackend(policy, tools=make_tools(tmp_path, version, **tools),
                              host_findings=host,
                              platform_facts=platform_facts or dict(VERIFIED))


def elf_spec(tmp_path: Path, name: str = "app") -> WorkloadSpec:
    exe = tmp_path / name
    exe.write_bytes(elf_bytes())
    exe.chmod(0o755)
    return WorkloadSpec("w", exe, [str(exe)], tmp_path, {"PATH": "/usr/bin:/bin"})


@pytest.fixture
def nothing_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything binds a coordinator listener or spawns a process."""
    def forbid(*args: object, **kwargs: object) -> Any:
        raise AssertionError("a coordinator listener or process was started")

    monkeypatch.setattr(dmtcp, "loopback_listener", forbid)
    monkeypatch.setattr(proc.ProcessTracker, "spawn", forbid)


# ------------------------------------------------------------ availability


def test_argv_always_joins_the_private_loopback_coordinator(tmp_path: Path) -> None:
    tools = dmtcp.DmtcpTools("/p/l", "/p/c", "/p/k", "/p/r")
    launch = dmtcp.launch_argv(tools, 4100, tmp_path / "c", tmp_path / "t", ["/bin/x", "-v"])
    restart = dmtcp.restart_argv(tools, 4101, tmp_path / "t", [tmp_path / "c" / "a.dmtcp"])
    for argv in (launch, restart, dmtcp.command_argv(tools, 4100, "--status")):
        assert argv[argv.index("--coord-host") + 1] == "127.0.0.1"
    assert "--join-coordinator" in launch and "--join-coordinator" in restart
    assert dmtcp.coordinator_argv(tools, tmp_path / "p", tmp_path / "c", 4100)[1:3] == [
        "--coord-port", "4100"]
    assert dmtcp.parse_version("dmtcp_launch (DMTCP) 4.2.0\nLicense") == "4.2.0"
    assert dmtcp.parse_version("") is None


def test_missing_tools_are_unavailable(tmp_path: Path) -> None:
    b = dmtcp.DmtcpBackend(tools=None, platform_facts=dict(VERIFIED))
    b.tools = None
    caps = b.probe()
    assert not caps.available and not caps.verified and "not found" in caps.notes[0]
    with pytest.raises(BackendError) as err:
        b.launch(elf_spec(tmp_path), tmp_path / "s", time.monotonic() + 30)
    assert err.value.code is ErrorCode.BACKEND_UNAVAILABLE
    assert dmtcp.find_tools(tmp_path / "nowhere") is None


def test_version_mismatch_is_unavailable_and_refused(tmp_path: Path, nothing_starts: None) -> None:
    b = backend(tmp_path, DEV, version="4.1.0")
    caps = b.probe()
    assert caps.version == "4.1.0" and not caps.available and not caps.verified
    assert any("UNANALYSED_VERSION" in n for n in caps.notes)
    with pytest.raises(BackendError) as err:
        b.launch(elf_spec(tmp_path), tmp_path / "s", time.monotonic() + 30)
    assert err.value.code is ErrorCode.BACKEND_UNAVAILABLE


def test_verified_only_on_the_stage02_platform(tmp_path: Path) -> None:
    caps = backend(tmp_path).probe()
    assert caps.available and caps.verified
    assert caps.verified_classes == ["c-single", "c-threads", "c-file-static", "c-file-grow"]
    other = backend(tmp_path / "x", platform_facts={**VERIFIED, "os_version_id": "22.04"})
    assert other.probe().available and not other.probe().verified
    unpinned = dmtcp.DmtcpBackend(tools=make_tools(tmp_path / "y", commit="0" * 40),
                                  platform_facts=dict(VERIFIED))
    caps = unpinned.probe()
    assert caps.available and not caps.verified and caps.source_commit == "0" * 40
    assert any("not the pinned" in n for n in caps.notes)
    assert backend(tmp_path / "z").probe().source_commit == dmtcp.PINNED_COMMIT


# ------------------------------------------------------- refusals (no spawn)


def test_default_policy_refuses_before_anything_starts(tmp_path: Path,
                                                       nothing_starts: None) -> None:
    for b in (dmtcp.DmtcpBackend(tools=make_tools(tmp_path), platform_facts=dict(VERIFIED)),
              backend(tmp_path / "b", CoordinatorPolicy.deny())):
        with pytest.raises(BackendError) as err:
            b.launch(elf_spec(tmp_path), tmp_path / "s", time.monotonic() + 30)
        assert err.value.code is ErrorCode.PERMISSION_DENIED


def test_developer_mode_refuses_a_multi_user_host(tmp_path: Path, nothing_starts: None) -> None:
    b = backend(tmp_path, DEV, host=lambda uid: ["processes of other regular users: uids [1001]"])
    with pytest.raises(BackendError) as err:
        b.launch(elf_spec(tmp_path), tmp_path / "s", time.monotonic() + 30)
    assert err.value.code is ErrorCode.PERMISSION_DENIED and "single-user" in err.value.message


def test_listener_baseline_failure_refuses(tmp_path: Path, nothing_starts: None,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    def no_audit() -> netaudit.ListenerGuard:
        raise netaudit.AuditError("`ss` not found")

    monkeypatch.setattr(dmtcp, "ListenerGuard", no_audit)
    with pytest.raises(BackendError) as err:
        backend(tmp_path, DEV).launch(elf_spec(tmp_path), tmp_path / "s", time.monotonic() + 30)
    assert err.value.code is ErrorCode.PERMISSION_DENIED and "baseline" in err.value.message


@pytest.mark.parametrize("name", ["python3.12", "python3"])
def test_unsupported_workload_is_refused_before_launch(tmp_path: Path, nothing_starts: None,
                                                       name: str) -> None:
    with pytest.raises(BackendError) as err:
        backend(tmp_path, DEV).launch(elf_spec(tmp_path, name), tmp_path / "s",
                                      time.monotonic() + 30)
    assert err.value.code is ErrorCode.UNSUPPORTED_WORKLOAD
    assert "KNOWN_RESTORE_FAILURE" in err.value.message


def capture_in(tmp_path: Path, b: dmtcp.DmtcpBackend) -> tuple[Path, Any]:
    d = tmp_path / "ckpt"
    d.mkdir(mode=0o700)
    image = d / "ckpt_app_1.dmtcp"
    image.write_bytes(b"x" * 64)
    image.chmod(0o600)
    entries = artifacts.inspect_all(d, [(image.name, ArtifactRole.IMAGE)])
    return d, b.capture_record(CaptureResult(entries, {"expected_processes": 1}, 1, ""))


def test_restore_refuses_tampered_or_incompatible_captures(tmp_path: Path,
                                                           nothing_starts: None) -> None:
    b = backend(tmp_path, DEV)
    d, record = capture_in(tmp_path, b)
    opts = RestoreOptions(session_dir=tmp_path / "r")
    cases = [
        (record.__class__(**{**vars(record), "backend_version": "4.1.0"}),
         ErrorCode.VERSION_MISMATCH),
        (record.__class__(**{**vars(record), "tool_sha256": {"dmtcp_launch": "0" * 64}}),
         ErrorCode.VERSION_MISMATCH),
        (record.__class__(**{**vars(record), "arch": "x86_64"}), ErrorCode.ARCH_MISMATCH),
    ]
    for bad, code in cases:
        with pytest.raises(BackendError) as err:
            b.restore(bad, d, opts, time.monotonic() + 30)
        assert err.value.code is code
    (d / "ckpt_app_1.dmtcp").write_bytes(b"y" * 64)  # tampered after capture
    with pytest.raises(BackendError) as err:
        b.restore(record, d, opts, time.monotonic() + 30)
    assert err.value.code is ErrorCode.CORRUPT
    (d / "ckpt_app_1.dmtcp").unlink()
    (d / "ckpt_app_1.dmtcp").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(BackendError) as err:
        b.restore(record, d, opts, time.monotonic() + 30)
    assert err.value.code is ErrorCode.INSECURE_ARTIFACT


def test_failed_checkpoint_removes_only_temporary_files(tmp_path: Path) -> None:
    d = tmp_path / "ckpt"
    d.mkdir(mode=0o700)
    for name in ("ckpt_a.dmtcp", "ckpt_a.dmtcp.temp", "x.tmp", "dmtcp_restart_script_1.sh"):
        (d / name).write_bytes(b"1")
    live = cast(dmtcp._Live, type("L", (), {"ckpt_dir": d})())
    dmtcp.DmtcpBackend._discard_partial(live)
    assert sorted(p.name for p in d.iterdir()) == ["ckpt_a.dmtcp", "dmtcp_restart_script_1.sh"]


def test_checkpoint_completion_timeout_is_reported(tmp_path: Path) -> None:
    now = [0.0]

    def sleep(s: float) -> None:
        now[0] += s

    waited = wait_for_checkpoint(tmp_path, 4100, 0, 1, timeout=5.0, clock=lambda: now[0],
                                 sleep=sleep)
    assert not waited.ok and waited.reason.startswith("timed out")
    dead = wait_for_checkpoint(tmp_path, 4100, 0, 1, timeout=5.0, clock=lambda: now[0],
                               sleep=sleep, liveness=lambda: "workload died")
    assert not dead.ok and dead.reason == "workload died"


def test_security_gates_unchanged_and_coordinators_developer_only() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "experiments" / "stage02"))
    from ngexp import backends as stage02

    assert stage02.DMTCP_LOOPBACK_ONLY_COORDINATOR_VERIFIED is False
    assert stage02.CONTROLLED_TRIAL_CLASSES == ("c-single",)
    src = Path(__file__).resolve().parents[2] / "src" / "napguard"
    texts = {p.relative_to(src).as_posix(): p.read_text(encoding="utf-8")
             for p in src.rglob("*.py")}
    assert not [p for p, t in texts.items() if "ngexp" in t.split('"""', 2)[-1]]
    permissive = [p for p, t in texts.items() if "experimental_single_user(" in t]
    assert sorted(permissive) == ["checkpoint/policy.py", "cli.py"]
    assert dmtcp.DmtcpBackend(tools=None).policy == CoordinatorPolicy.deny()


# ------------------------------------------ Linux: fake coordinators, real audits

GOOD_COORDINATOR = f"""#!{sys.executable}
import socket, sys, time
args = sys.argv[1:]
listener = socket.socket(fileno=821)          # inherited, pre-bound by NapGuard
with open(args[args.index("--port-file") + 1], "w") as fh:
    fh.write(str(listener.getsockname()[1]))
time.sleep(60)
"""

# Ignores the hand-off and falls back to INADDR_ANY on --coord-port (must fail).
FALLBACK_COORDINATOR = f"""#!{sys.executable}
import socket, sys, time
args = sys.argv[1:]
port = int(args[args.index("--coord-port") + 1])
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", port))
s.listen(1)
with open(args[args.index("--port-file") + 1], "w") as fh:
    fh.write(str(port))
time.sleep(60)
"""

# Adopts the loopback listener but ALSO opens a wildcard listener on another port.
EXPOSING_COORDINATOR = f"""#!{sys.executable}
import socket, sys, time
args = sys.argv[1:]
listener = socket.socket(fileno=821)
extra = socket.socket()
extra.bind(("0.0.0.0", 0))
extra.listen(1)
with open(args[args.index("--port-file") + 1], "w") as fh:
    fh.write(str(listener.getsockname()[1]))
time.sleep(60)
"""

# --version, else skip the 9 DMTCP options and run the workload in this process.
EXEC_LAUNCH = "shift 9\nexec \"$@\"\n"
FAILING_RESTART = "echo 'restart failed on purpose' >&2\nexit 3\n"


def linux_backend(tmp_path: Path, coordinator: str, **tools: str) -> dmtcp.DmtcpBackend:
    facts = {**VERIFIED, "arch": platform.machine()}
    return backend(tmp_path, DEV, coordinator=coordinator, platform_facts=facts, **tools)


def sleep_spec(tmp_path: Path) -> WorkloadSpec:
    exe = Path("/bin/sleep").resolve()
    return WorkloadSpec("w", exe, [str(exe), "60"], tmp_path, proc.filtered_env())


def recorded_alive(session_dir: Path) -> list[str]:
    state = proc.SessionState.read(session_dir / "session.json")
    return [r["name"] for r in state.processes
            if proc.alive(proc.ProcId(r["pid"], r["start_ticks"], r["uid"]))]


def no_new_exposure(guard: netaudit.ListenerGuard) -> bool:
    """No new non-loopback listener except provable host sockets of system accounts."""
    return guard.violations(set(), "after test") == []


@linux_only
def test_coordinator_ignoring_the_handoff_fails_closed(tmp_path: Path) -> None:
    guard = netaudit.ListenerGuard()
    b = linux_backend(tmp_path, FALLBACK_COORDINATOR)
    with pytest.raises(BackendError) as err:
        b.launch(sleep_spec(tmp_path), tmp_path / "s", time.monotonic() + 30)
    assert err.value.code is ErrorCode.PERMISSION_DENIED
    assert "did not adopt the loopback listener" in err.value.message
    assert no_new_exposure(guard)
    assert proc.leaked_processes(tmp_path / "s") == []


@linux_only
def test_exposed_coordinator_is_killed(tmp_path: Path) -> None:
    guard = netaudit.ListenerGuard()
    b = linux_backend(tmp_path, EXPOSING_COORDINATOR)
    with pytest.raises(BackendError) as err:
        b.launch(sleep_spec(tmp_path), tmp_path / "s", time.monotonic() + 30)
    assert err.value.code is ErrorCode.PERMISSION_DENIED
    assert "beyond loopback" in err.value.message
    assert no_new_exposure(guard)  # the exposing coordinator was killed
    assert proc.leaked_processes(tmp_path / "s") == []


@linux_only
def test_checkpoint_timeout_leaves_workload_running_then_cleanup(tmp_path: Path) -> None:
    b = linux_backend(tmp_path, GOOD_COORDINATOR, launch=EXEC_LAUNCH)
    session = b.launch(sleep_spec(tmp_path), tmp_path / "s", time.monotonic() + 30)
    try:
        assert b.status(session).state is SessionState.ALIVE
        b.audit(session)  # coordinator on loopback only: passes
        with pytest.raises(BackendError) as err:
            b.checkpoint(session, tmp_path / "elsewhere", CheckpointOptions(),
                         time.monotonic() + 30)
        assert err.value.code is ErrorCode.INVALID_ARGUMENT  # images stay where DMTCP writes
        with pytest.raises(BackendError) as err:
            b.checkpoint(session, Path(session.data["checkpoint_dir"]), CheckpointOptions(),
                         time.monotonic() + 3)
        assert err.value.code in (ErrorCode.TIMED_OUT, ErrorCode.CHECKPOINT_FAILED)
        assert b.status(session).state is SessionState.ALIVE  # workload left running
    finally:
        assert b.cleanup(session, None) == []
    assert b.cleanup(session, None) == [] and b.close_all() == []  # safe to repeat
    assert recorded_alive(tmp_path / "s") == []


@linux_only
def test_failed_restore_reports_diagnostics_and_cleans_up(tmp_path: Path) -> None:
    b = linux_backend(tmp_path, GOOD_COORDINATOR, restart=FAILING_RESTART)
    d, record = capture_in(tmp_path, b)
    with pytest.raises(BackendError) as err:
        b.restore(record, d, RestoreOptions(session_dir=tmp_path / "r"), time.monotonic() + 30)
    assert err.value.code is ErrorCode.RESTORE_FAILED
    assert err.value.detail["restarter_exit"]["exit_code"] == 3
    assert "restart failed on purpose" in err.value.detail["restarter_stderr_tail"]
    assert recorded_alive(tmp_path / "r") == []  # coordinator stopped, nothing left
    assert b.close_all() == []
