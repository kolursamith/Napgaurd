"""Packaging (sandboxed HOME), the new daemon IPC methods, and a GUI smoke test.

The IPC tests use the labelled TEST DOUBLE backend; the GUI smoke test needs Tkinter and a
display, so it runs on the Ubuntu desktop (scripts/final-ubuntu-check.sh) and is skipped
elsewhere.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from mvp_fakes import FakeBackend, fake_build

from napguard.daemon.ipc import Client, IpcError
from napguard.daemon.service import Service, wait_operation
from napguard.power.battery import BatteryMonitor

ROOT = Path(__file__).resolve().parents[2]
HAS_GUI = importlib.util.find_spec("tkinter") is not None and bool(os.environ.get("DISPLAY"))


@pytest.fixture
def short_dir() -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(prefix="ng-", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


FAKE_SYSTEMCTL = """#!/bin/sh
# Test stub: logs every call; answers FragmentPath from $FAKE_FRAGMENT (empty by default).
echo "$@" >> "$SYSTEMCTL_LOG"
case "$*" in *FragmentPath*) printf '%s\\n' "${FAKE_FRAGMENT:-}" ;; esac
exit 0
"""


def sandbox_env(base: Path) -> dict[str, str]:
    """A sandboxed user: HOME, XDG data/config/RUNTIME dirs, and a stub systemctl, so the
    scripts can never see the real user's daemon or touch the real systemd user manager."""
    home = base / "home"
    home.mkdir()
    runtime = base / "run"
    runtime.mkdir(mode=0o700)
    stubs = base / "stubs"
    stubs.mkdir()
    (stubs / "systemctl").write_text(FAKE_SYSTEMCTL)
    (stubs / "systemctl").chmod(0o755)
    return dict(os.environ, HOME=str(home), XDG_DATA_HOME=str(home / ".local" / "share"),
                XDG_CONFIG_HOME=str(home / ".config"), XDG_RUNTIME_DIR=str(runtime),
                SYSTEMCTL_LOG=str(base / "systemctl.log"),
                PATH=f"{stubs}{os.pathsep}{Path(sys.executable).parent}{os.pathsep}"
                     f"{os.environ['PATH']}")


def sh(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", *args], env=env, capture_output=True, text=True, timeout=120)


def test_install_uninstall_and_purge_in_a_sandbox(short_dir: Path) -> None:
    env = sandbox_env(short_dir)
    home = Path(env["HOME"])
    done = subprocess.run(["bash", str(ROOT / "scripts" / "install.sh"), "--with-desktop",
                           "--with-systemd"], env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    manifest = (home / ".local/share/napguard/install-manifest.txt").read_text().split()
    assert len(manifest) == 5 and all(Path(p).is_file() for p in manifest)
    unit = (home / ".config/systemd/user/napguard.service").read_text()
    assert "--i-accept-single-user-coordinator" not in unit.split("ExecStart=")[1].splitlines()[0]
    desktop = (home / ".local/share/applications/napguard.desktop").read_text()
    assert "Exec=" in desktop and "gui" in desktop
    run = subprocess.run([str(home / ".local/bin/napguard"), "--socket", str(short_dir / "x.sock"),
                          "status"], env=env, capture_output=True, text=True, timeout=60)
    assert run.returncode == 2 and "DAEMON_UNAVAILABLE" in run.stderr  # the wrapper works
    (home / ".local/bin/mine").write_text("user file")
    data = home / ".local/share/napguard/workloads"
    data.mkdir(parents=True)
    (data / "keep").write_text("x")
    subprocess.run(["bash", str(ROOT / "scripts" / "uninstall.sh")], env=env, check=True,
                   capture_output=True, timeout=60)
    assert not any(Path(p).exists() for p in manifest)
    assert (home / ".local/bin/mine").exists() and (data / "keep").exists()  # data kept
    subprocess.run(["bash", str(ROOT / "scripts" / "uninstall.sh"), "--purge", "--yes"], env=env,
                   check=True, capture_output=True, timeout=60)
    assert not (home / ".local/share/napguard").exists() and (home / ".local/bin/mine").exists()


def test_install_refuses_to_overwrite_a_foreign_file(short_dir: Path) -> None:
    env = sandbox_env(short_dir)
    target = Path(env["HOME"]) / ".local/bin/napguard"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\necho someone else's tool\n")
    done = subprocess.run(["bash", str(ROOT / "scripts" / "install.sh")], env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode != 0 and "refusing to overwrite" in done.stderr
    assert "someone else's tool" in target.read_text()


def start(short_dir: Path) -> tuple[Service, Client]:
    service = Service(short_dir / "data", short_dir / "run" / "daemon.sock", FakeBackend(),
                      coordinator_acknowledged=True, build=fake_build, require_persistent=False,
                      battery=BatteryMonitor(short_dir / "none"), peer=lambda s: os.getuid(),
                      scheduler_tick_s=3600, battery_poll_s=3600,
                      manager_options={"verify_window_s": 0.2, "report_timeout_s": 10})
    service.start()
    return service, Client(short_dir / "run" / "daemon.sock", timeout=10)


def op(client: Client, method: str, **params: object) -> dict[str, object]:
    accepted = client.call(method, **params)
    return wait_operation(client.call, accepted["operation_id"], timeout=60, poll=0.05)


def test_history_scheduler_diagnostics_and_shutdown_over_ipc(short_dir: Path) -> None:
    service, client = start(short_dir)
    try:
        with pytest.raises(IpcError) as err:
            client.call("scheduler.run_now")
        assert err.value.code == "NOT_RUNNING"
        assert op(client, "workloads.launch_demo", workload_class="c-threads")["state"] == \
            "SUCCEEDED"
        wid = client.call("workloads.list")[0]["id"]
        status = client.call("scheduler.status")
        assert status["eligible"][0]["workload_id"] == wid and status["enabled"] is False
        ran = client.call("scheduler.run_now")
        result = wait_operation(client.call, ran["operation_ids"][0], 60, 0.05)
        assert result["state"] == "SUCCEEDED" and result["trigger"] == "scheduler_run_now"
        assert client.call("operations.list", kind="checkpoint", trigger="scheduler")
        refused = op(client, "history.archive", workload_id=wid, archived=True)
        assert refused["state"] == "FAILED" and refused["error_code"] == "CONFLICT"
        time.sleep(0.2)
        assert op(client, "workloads.stop", workload_id=wid)["state"] == "SUCCEEDED"
        preview = client.call("history.preview")
        assert [e["workload_id"] for e in preview["eligible"]] == [wid]
        with pytest.raises(IpcError):
            client.call("history.delete", workload_ids=[wid], confirm=False)
        with pytest.raises(IpcError):
            client.call("history.delete", workload_ids=[5], confirm=True)  # wrong element type
        assert op(client, "history.archive", workload_id=wid, archived=True)["state"] == \
            "SUCCEEDED"
        assert client.call("workloads.list") == []
        assert len(client.call("workloads.list", include_archived=True)) == 1
        exported = client.call("diagnostics.export")
        path = Path(exported["path"])
        assert path.stat().st_mode & 0o777 == 0o600
        assert json.loads(path.read_text())["workloads"][0]["id"] == wid
        assert op(client, "history.reset_demo", confirm=True)["state"] == "SUCCEEDED"
        assert client.call("workloads.list", include_archived=True) == []
        status_all = client.call("daemon.status")
        assert status_all["data_dir"] and status_all["socket"] and status_all["supported_classes"]
        assert client.call("daemon.shutdown", confirm=True) == {"stopping": True}
        assert service.shutdown_requested.is_set()
    finally:
        service.shutdown()


@pytest.mark.skipif(not HAS_GUI, reason="needs Tkinter and a display (Ubuntu desktop)")
def test_gui_smoke_renders_every_screen(short_dir: Path) -> None:
    service, _ = start(short_dir)
    try:
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"),
                   NAPGUARD_GUI_CONFIG=str(short_dir / "gui.json"))
        done = subprocess.run([sys.executable, "-m", "napguard.main", "--socket",
                               str(short_dir / "run" / "daemon.sock"), "gui", "--smoke"],
                              env=env, capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stdout + done.stderr
        assert "9 screens rendered; daemon connected" in done.stdout
        assert "post-launch selection and heartbeat responsive" in done.stdout
    finally:
        service.shutdown()


def test_cli_parity_for_history_and_operations(short_dir: Path,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    from napguard import main as cli

    service, _ = start(short_dir)
    sock = ["--socket", str(short_dir / "run" / "daemon.sock")]
    try:
        assert cli.main([*sock, "launch-demo", "--class", "c-threads"]) == 0
        assert cli.main([*sock, "run-scheduler"]) == 0
        capsys.readouterr()
        assert cli.main([*sock, "--json", "workloads"]) == 0
        wid = json.loads(capsys.readouterr().out)[0]["id"]
        assert cli.main([*sock, "archive", wid]) == 1  # live: refused
        assert cli.main([*sock, "stop", wid]) == 0
        assert cli.main([*sock, "history"]) == 0
        assert f"deletable {wid}" in capsys.readouterr().out
        for needs_yes in ("clear-history", "reset-demo", "shutdown"):
            assert cli.main([*sock, needs_yes]) == 2
        assert cli.main([*sock, "archive", wid]) == 0
        assert cli.main([*sock, "unarchive", wid]) == 0
        assert cli.main([*sock, "operations", "--limit", "5"]) == 0
        assert cli.main([*sock, "diagnostics"]) == 0
        assert cli.main([*sock, "clear-history", "--yes"]) == 0
        assert service.manager.workloads(include_archived=True) == []
        assert cli.main([*sock, "reset-demo", "--yes"]) == 0
    finally:
        service.shutdown()



# ------------------------------------------- Ubuntu 87b89a1 regression: purge and systemd


def installed(short_dir: Path) -> tuple[dict[str, str], Path]:
    env = sandbox_env(short_dir)
    done = sh(env, str(ROOT / "scripts" / "install.sh"), "--with-systemd")
    assert done.returncode == 0, done.stdout + done.stderr
    data = Path(env["XDG_DATA_HOME"]) / "napguard"
    (data / "workloads").mkdir(parents=True, exist_ok=True)
    (data / "workloads" / "f").write_text("x")
    return env, data


def test_purge_is_not_blocked_by_another_daemons_socket(short_dir: Path) -> None:
    # On Ubuntu the user's real GUI daemon socket in $XDG_RUNTIME_DIR made the purge refuse.
    import socket as socketlib

    env, data = installed(short_dir)
    (Path(env["XDG_RUNTIME_DIR"]) / "napguard").mkdir()
    other = socketlib.socket(socketlib.AF_UNIX)
    other.bind(str(Path(env["XDG_RUNTIME_DIR"]) / "napguard" / "daemon.sock"))
    try:
        done = sh(env, str(ROOT / "scripts" / "uninstall.sh"), "--purge", "--yes")
        assert done.returncode == 0, done.stdout + done.stderr
        assert not data.exists()
    finally:
        other.close()


def test_purge_refuses_while_a_daemon_uses_this_data_directory(short_dir: Path) -> None:
    env, data = installed(short_dir)
    holder = subprocess.Popen([sys.executable, "-c", (
        "import fcntl, os, sys, time; fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600);"
        " fcntl.flock(fd, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(60)"),
        str(data / "daemon.lock")], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        done = sh(env, str(ROOT / "scripts" / "uninstall.sh"), "--purge", "--yes")
        assert done.returncode == 1 and "is using" in done.stderr
        assert (data / "workloads" / "f").exists()  # nothing purged
    finally:
        holder.kill()
        holder.wait()
    done = sh(env, str(ROOT / "scripts" / "uninstall.sh"), "--purge", "--yes")
    assert done.returncode == 0 and not data.exists()


def test_purge_still_needs_explicit_confirmation(short_dir: Path) -> None:
    env, data = installed(short_dir)
    done = subprocess.run(["bash", str(ROOT / "scripts" / "uninstall.sh"), "--purge"], env=env,
                          input="no\n", capture_output=True, text=True, timeout=60)
    assert done.returncode == 1 and "not purged" in done.stdout and data.exists()


@pytest.mark.parametrize("ours", [False, True])
def test_uninstall_touches_systemd_only_for_its_own_unit(short_dir: Path, ours: bool) -> None:
    env, _ = installed(short_dir)
    unit = Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user" / "napguard.service"
    env["FAKE_FRAGMENT"] = str(unit) if ours else "/home/someone/.config/systemd/user/napguard.service"
    done = sh(env, str(ROOT / "scripts" / "uninstall.sh"))
    assert done.returncode == 0, done.stdout + done.stderr
    calls = Path(env["SYSTEMCTL_LOG"]).read_text().splitlines()
    acting = [c for c in calls if "FragmentPath" not in c]
    if ours:
        assert acting == ["--user stop napguard.service", "--user disable napguard.service",
                          "--user daemon-reload"]
    else:
        assert acting == []  # a foreign or real unit is never stopped or reloaded
    assert not unit.exists()


def test_one_daemon_per_data_directory(short_dir: Path) -> None:
    first, _ = start(short_dir)
    try:
        with pytest.raises(IpcError) as err:
            Service(short_dir / "data", short_dir / "run2" / "d.sock", FakeBackend(),
                    coordinator_acknowledged=False, build=fake_build, require_persistent=False)
        assert err.value.code == "ALREADY_RUNNING"
    finally:
        first.shutdown()
    again = Service(short_dir / "data", short_dir / "run2" / "d.sock", FakeBackend(),
                    coordinator_acknowledged=False, build=fake_build, require_persistent=False)
    again.shutdown()  # the lock was released by the first shutdown
