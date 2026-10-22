"""MVP Stage 06: the user-level daemon over a real Unix socket, with the labelled TEST
DOUBLE backend (mvp_fakes.py). Linux-only tests use the kernel's real SO_PEERCRED."""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import struct
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from mvp_fakes import FakeBackend, fake_build

from napguard import main as cli
from napguard.daemon import ipc
from napguard.daemon.ipc import Client, IpcError
from napguard.daemon.service import Service, validate, wait_operation
from napguard.power.battery import BatteryMonitor
from napguard.ui import presenter as view

linux_only = pytest.mark.skipif(platform.system() != "Linux", reason="needs SO_PEERCRED")


@pytest.fixture
def short_dir() -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(prefix="ng-", dir="/tmp"))  # AF_UNIX paths are short
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def start(short_dir: Path, *, peer_uid: int | None = None, acknowledged: bool = True,
          mode: str = "genuine") -> tuple[Service, Client]:
    sock = short_dir / "run" / "daemon.sock"
    service = Service(short_dir / "data", sock, FakeBackend(mode),
                      coordinator_acknowledged=acknowledged, build=fake_build,
                      require_persistent=False, battery=BatteryMonitor(short_dir / "nobattery"),
                      peer=(lambda s: os.getuid()) if peer_uid is None else (lambda s: peer_uid),
                      scheduler_tick_s=0.2, battery_poll_s=3600,
                      manager_options={"verify_window_s": 0.3, "report_timeout_s": 10})
    service.start()
    return service, Client(sock, timeout=10)


def op(client: Client, method: str, **params: object) -> dict[str, object]:
    accepted = client.call(method, **params)
    assert accepted["state"] == "ACCEPTED"
    return wait_operation(client.call, accepted["operation_id"], timeout=60, poll=0.05)


def test_end_to_end_over_ipc(short_dir: Path) -> None:
    service, client = start(short_dir)
    try:
        status = client.call("daemon.status")
        assert status["production_authorized"] is False and "developer" in status["coordinator_mode"]
        launched = op(client, "workloads.launch_demo", workload_class="c-threads")
        assert launched["state"] == "SUCCEEDED", launched
        wid = client.call("workloads.list")[0]["id"]
        ck = op(client, "checkpoint.request", workload_id=wid)
        assert ck["state"] == "SUCCEEDED", ck
        cid = ck["result"]["checkpoint_id"]  # type: ignore[index]
        refused = op(client, "recovery.restore", checkpoint_id=cid, confirm=True)
        assert refused["state"] == "FAILED" and refused["error_code"] == "CONFLICT"
        assert op(client, "workloads.stop", workload_id=wid)["state"] == "SUCCEEDED"
        assert op(client, "checkpoints.verify", checkpoint_id=cid)["state"] == "SUCCEEDED"
        restored = op(client, "recovery.restore", checkpoint_id=cid, confirm=True)
        assert restored["state"] == "SUCCEEDED", restored
        history = client.call("checkpoints.list", workload_id=wid)
        assert history[0]["restore_verified_utc"]
        kinds = {e["kind"] for e in client.call("events.list", limit=100)}
        assert {"daemon.started", "workload.launched", "checkpoint.published",
                "restore.verified"} <= kinds
    finally:
        service.shutdown()
    assert not (short_dir / "run" / "daemon.sock").exists()


def test_socket_and_data_are_private(short_dir: Path) -> None:
    service, client = start(short_dir)
    try:
        assert (short_dir / "run").stat().st_mode & 0o777 == 0o700
        assert (short_dir / "run" / "daemon.sock").stat().st_mode & 0o777 == 0o600
        assert (short_dir / "data").stat().st_mode & 0o777 == 0o700
        assert (short_dir / "data" / "catalog.db").stat().st_mode & 0o777 == 0o600
        with pytest.raises(IpcError) as err:
            Service(short_dir / "data2", short_dir / "run" / "daemon.sock", FakeBackend(),
                    coordinator_acknowledged=False, build=fake_build,
                    require_persistent=False).server.bind()
        assert err.value.code == "ALREADY_RUNNING"
    finally:
        service.shutdown()


def test_other_users_are_rejected(short_dir: Path) -> None:
    service, client = start(short_dir, peer_uid=os.getuid() + 1)
    try:
        with pytest.raises(IpcError) as err:
            client.call("daemon.status")
        assert err.value.code == "UNAUTHENTICATED"
        events = service.catalog.events(20)
        assert any(e["kind"] == "auth.rejected" for e in events)
    finally:
        service.shutdown()


def raw(sock_path: Path, payload: bytes) -> dict[str, object] | None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(str(sock_path))
    try:
        s.sendall(payload)
        return ipc.recv_frame(s)
    finally:
        s.close()


def test_bad_frames_and_requests_are_refused(short_dir: Path) -> None:
    service, client = start(short_dir)
    sock = short_dir / "run" / "daemon.sock"
    try:
        too_big = raw(sock, struct.pack(">I", ipc.MAX_FRAME + 1))
        assert too_big is not None and too_big["error"]["code"] == "FRAME_TOO_LARGE"  # type: ignore[index]
        garbage = raw(sock, struct.pack(">I", 3) + b"{x}")
        assert garbage is not None and garbage["error"]["code"] == "INVALID_ARGUMENT"  # type: ignore[index]
        body = json.dumps({"v": 2, "id": 1, "method": "daemon.status", "params": {}}).encode()
        wrong = raw(sock, struct.pack(">I", len(body)) + body)
        assert wrong is not None and wrong["error"]["code"] == "UNSUPPORTED_VERSION"  # type: ignore[index]
        cases: list[tuple[str, dict[str, object], str]] = [
            ("no.such", {}, "UNKNOWN_METHOD"),
            ("daemon.status", {"x": 1}, "INVALID_ARGUMENT"),
            ("workloads.stop", {}, "INVALID_ARGUMENT"),
            ("workloads.stop", {"workload_id": 5}, "INVALID_ARGUMENT"),
            ("workloads.launch_demo", {"workload_class": "py"}, "UNSUPPORTED_WORKLOAD"),
            ("recovery.restore", {"checkpoint_id": "ck-1", "confirm": False}, "INVALID_ARGUMENT"),
            ("settings.set", {"auto_checkpoint_interval_s": 10}, "INVALID_ARGUMENT"),
            ("settings.set", {"battery_low_percent": 5}, "INVALID_ARGUMENT"),
            ("battery.simulate", {"percent": 150}, "INVALID_ARGUMENT"),
        ]
        for method, params, code in cases:
            with pytest.raises(IpcError) as err:
                client.call(method, **params)
            assert err.value.code == code, (method, params)
    finally:
        service.shutdown()


def test_without_acknowledgement_coordinator_operations_fail(short_dir: Path) -> None:
    service, client = start(short_dir, acknowledged=False, mode="refuse-launch")
    try:
        assert "refused" in client.call("daemon.status")["coordinator_mode"]
        result = op(client, "workloads.launch_demo", workload_class="c-single")
        assert result["state"] == "FAILED" and result["error_code"] == "PERMISSION_DENIED"
    finally:
        service.shutdown()


def test_settings_scheduler_and_simulated_battery(short_dir: Path) -> None:
    service, client = start(short_dir)
    try:
        s = client.call("settings.set", auto_checkpoint_enabled=True,
                        auto_checkpoint_interval_s=60)
        assert s["auto_checkpoint_enabled"] is True
        real = client.call("battery.status")
        assert real["available"] is False and real["simulated"] is False
        assert op(client, "workloads.launch_demo", workload_class="c-threads")["state"] == \
            "SUCCEEDED"
        sim = client.call("battery.simulate", percent=15, ac_online=False)
        assert sim["source"] == "SIMULATED" and sim["simulated"] is True
        events = client.call("events.list", limit=50)
        battery_events = [e for e in events if e["kind"].startswith("battery.")]
        assert battery_events and all(e["simulated"] for e in battery_events)
        ops = service.catalog.query("SELECT trigger FROM operations WHERE kind='checkpoint'")
        assert {"trigger": "low_battery_simulated"} in ops
        client.call("battery.simulate", clear=True)
        assert client.call("battery.status")["simulated"] is False
    finally:
        service.shutdown()


def test_cli_reports_an_unavailable_daemon(short_dir: Path,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--socket", str(short_dir / "none.sock"), "status"]) == 2
    assert "DAEMON_UNAVAILABLE" in capsys.readouterr().err
    assert cli.main(["--socket", str(short_dir / "none.sock"), "restore", "ck-1"]) == 2


def test_cli_drives_the_daemon(short_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    service, _ = start(short_dir)
    sock = ["--socket", str(short_dir / "run" / "daemon.sock")]
    try:
        assert cli.main([*sock, "launch-demo", "--class", "c-threads"]) == 0
        assert cli.main([*sock, "--json", "workloads"]) == 0
        out = capsys.readouterr().out
        wid = json.loads(out[out.index("["):])[0]["id"]
        assert cli.main([*sock, "checkpoint", wid]) == 0
        assert cli.main([*sock, "battery", "--simulate", "50"]) == 0
        assert "SIMULATED" in capsys.readouterr().out
        assert cli.main([*sock, "status"]) == 0
        assert "production authorized: no" in capsys.readouterr().out
    finally:
        service.shutdown()


def test_schema_and_views() -> None:
    assert validate("events.list", {"limit": 5}) == {"limit": 5}
    with pytest.raises(IpcError):
        validate("events.list", {"limit": True})  # a bool is not an int
    with pytest.raises(IpcError):
        validate("workloads.stop", {"workload_id": "x" * 300})
    assert view.battery_long({"available": True, "simulated": True, "percent": 9,
                              "charging": False, "ac_online": False}).startswith("SIMULATED")
    assert view.event_text({"ts_utc": "t", "kind": "k", "message": "m", "simulated": True}) \
        == "[SIMULATED] m"
    assert view.human_bytes(25_059_040) == "23.9 MiB"


@linux_only
def test_real_peer_credentials_are_this_user(short_dir: Path) -> None:
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        assert ipc.peer_uid(a) == os.getuid()
    finally:
        a.close()
        b.close()
