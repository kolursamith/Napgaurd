"""Stage 03a: manifest validation, IPv4/IPv6 reachability audits, incomplete images,
GUI-toolkit refusal, and repeated cleanup. Portable unit tests (no DMTCP, no Linux)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from napguard.checkpoint import artifacts, dmtcp, manifest
from napguard.checkpoint.base import (
    ArtifactRole,
    BackendError,
    BackendSession,
    CaptureRecord,
    ErrorCode,
)
from napguard.checkpoint.compatibility import ReasonCode, RuntimeFacts, runtime_findings
from napguard.checkpoint.dmtcp_completion import checkpoint_evidence
from napguard.core import netaudit, proc

SHA = "a" * 64


def captured(tmp_path: Path) -> tuple[Path, CaptureRecord]:
    d = tmp_path / "ckpt"
    d.mkdir(mode=0o700)
    image = d / "ckpt_app_1.dmtcp"
    image.write_bytes(b"image")
    image.chmod(0o600)
    entries = artifacts.inspect_all(d, [(image.name, ArtifactRole.IMAGE)])
    record = CaptureRecord("dmtcp", "4.2.0", {"dmtcp_launch": SHA}, "aarch64", "6.8.0",
                           entries, {"expected_processes": 1, "filesystem": "ext4"})
    return d, record


# ------------------------------------------------------------------ manifest


def test_manifest_round_trip_states_what_is_not_restored(tmp_path: Path) -> None:
    d, record = captured(tmp_path)
    path = manifest.write_manifest(d, record)
    assert path.stat().st_mode & 0o777 == 0o600
    assert manifest.read_manifest(d) == record
    text = path.read_text(encoding="utf-8")
    assert "NOT rolled back" in text and "LIM-007" in text
    with pytest.raises(BackendError):
        manifest.write_manifest(d, record)  # never overwritten


def mutate(d: Path, change: Any) -> None:
    path = d / manifest.MANIFEST_NAME
    body = json.loads(path.read_text(encoding="utf-8"))
    change(body)
    path.write_text(json.dumps(body), encoding="utf-8")


def resign(body: dict[str, Any]) -> dict[str, Any]:
    """Recompute the digest so only the structural validation can refuse the edit."""
    rest = {k: v for k, v in body.items() if k != "body_sha256"}
    body["body_sha256"] = hashlib.sha256(manifest._canonical(rest)).hexdigest()
    return body


@pytest.mark.parametrize("change", [
    lambda b: b.__setitem__("arch", "x86_64"),  # edited: digest mismatch
    lambda b: (b.pop("limitations"), resign(b)),  # incomplete
    lambda b: (b.__setitem__("extra", 1), resign(b)),  # unknown field
    lambda b: (b.__setitem__("limitations", ["files ARE rolled back"]), resign(b)),
    lambda b: (b["artifacts"][0].__setitem__("name", "../../etc/passwd"), resign(b)),
    lambda b: (b["artifacts"][0].__setitem__("name", "/abs/path"), resign(b)),
    lambda b: (b["artifacts"][0].__setitem__("role", "SHELL"), resign(b)),
    lambda b: (b["artifacts"][0].__setitem__("role", "METADATA"), resign(b)),  # no image
    lambda b: (b["artifacts"].append(dict(b["artifacts"][0])), resign(b)),  # duplicate
    lambda b: (b["artifacts"][0].__setitem__("sha256", "zz"), resign(b)),
    lambda b: (b.__setitem__("schema", "v0"), resign(b)),
    lambda b: (b["tool_sha256"].clear(), resign(b)),
])
def test_invalid_or_incomplete_manifests_are_refused(tmp_path: Path, change: Any) -> None:
    d, record = captured(tmp_path)
    manifest.write_manifest(d, record)
    mutate(d, change)
    with pytest.raises(BackendError) as err:
        manifest.read_manifest(d)
    assert err.value.code is ErrorCode.INVALID_MANIFEST


def test_missing_garbled_symlinked_or_exposed_manifest_is_refused(tmp_path: Path) -> None:
    d, record = captured(tmp_path)
    with pytest.raises(BackendError) as err:
        manifest.read_manifest(d)
    assert err.value.code is ErrorCode.INVALID_MANIFEST  # missing
    path = d / manifest.MANIFEST_NAME
    path.write_text("{not json", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(BackendError) as err:
        manifest.read_manifest(d)
    assert err.value.code is ErrorCode.INVALID_MANIFEST
    path.chmod(0o644)
    with pytest.raises(BackendError) as err:
        manifest.read_manifest(d)
    assert err.value.code is ErrorCode.INSECURE_ARTIFACT
    path.unlink()
    elsewhere = tmp_path / "other.json"
    manifest.write_manifest(d, record)
    (d / manifest.MANIFEST_NAME).rename(elsewhere)
    path.symlink_to(elsewhere)
    with pytest.raises(BackendError) as err:
        manifest.read_manifest(d)
    assert err.value.code is ErrorCode.INSECURE_ARTIFACT


def test_manifest_digest_does_not_vouch_for_changed_images(tmp_path: Path) -> None:
    d, record = captured(tmp_path)
    manifest.write_manifest(d, record)
    (d / "ckpt_app_1.dmtcp").write_bytes(b"imagf")  # the image changes, not the manifest
    with pytest.raises(BackendError) as err:
        artifacts.verify(d, manifest.read_manifest(d).artifacts)
    assert err.value.code is ErrorCode.CORRUPT


def test_filesystem_type_reads_the_longest_mount(tmp_path: Path) -> None:
    mounts = tmp_path / "mounts"
    mounts.write_text(f"/dev/vda2 / ext4 rw 0 0\ntmpfs {tmp_path} tmpfs rw 0 0\n",
                      encoding="utf-8")
    assert artifacts.filesystem_type(tmp_path / "x", str(mounts)) == "tmpfs"
    assert artifacts.filesystem_type(Path("/var"), str(mounts)) == "ext4"
    assert artifacts.filesystem_type(tmp_path, str(tmp_path / "absent")) == "?"
    assert "tmpfs" not in artifacts.PERSISTENT_FS


# ------------------------------------------------- IPv4/IPv6 reachability audit


def coordinator() -> proc.ManagedProcess:
    return proc.ManagedProcess("coordinator", proc.ProcId(4242, 1, 1000))


@pytest.mark.parametrize(("listeners", "addresses", "reachable"), [
    ([("127.0.0.1:4100", (4242,))], (["192.168.64.3"], []), {"192.168.64.3"}),  # IPv4 reaches
    ([("127.0.0.1:4100", (4242,))], ([], ["2001:db8::5"]), {"2001:db8::5"}),  # IPv6 reaches
    ([("127.0.0.1:4100", (4242,)), ("[::]:4100", (4242,))], ([], []), set()),  # wildcard v6
    ([("0.0.0.0:5000", (4242,)), ("127.0.0.1:4100", (4242,))], ([], []), set()),  # other port
    ([], ([], []), set()),  # nothing listening on the handed-over port
])
def test_isolation_failures_are_refused(monkeypatch: pytest.MonkeyPatch,
                                        listeners: list[tuple[str, tuple[int, ...]]],
                                        addresses: tuple[list[str], list[str]],
                                        reachable: set[str]) -> None:
    monkeypatch.setattr(dmtcp, "current_listeners",
                        lambda: [netaudit.Listener("tcp", a, p) for a, p in listeners])
    monkeypatch.setattr(dmtcp, "non_loopback_ipv4_addresses", lambda: addresses[0])
    monkeypatch.setattr(dmtcp, "non_loopback_ipv6_addresses", lambda: addresses[1])
    monkeypatch.setattr(dmtcp, "tcp_reachable", lambda host, port: host in reachable)
    with pytest.raises(BackendError) as err:
        dmtcp.DmtcpBackend._verify_isolation(coordinator(), 4100, "coordinator")
    assert err.value.code is ErrorCode.PERMISSION_DENIED


def test_isolation_passes_only_when_loopback_only_and_unreachable(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dmtcp, "current_listeners",
                        lambda: [netaudit.Listener("tcp", "127.0.0.1:4100", (4242,))])
    monkeypatch.setattr(dmtcp, "non_loopback_ipv4_addresses", lambda: ["192.168.64.3"])
    monkeypatch.setattr(dmtcp, "non_loopback_ipv6_addresses", lambda: ["2001:db8::5"])
    monkeypatch.setattr(dmtcp, "tcp_reachable", lambda host, port: False)
    facts = dmtcp.DmtcpBackend._verify_isolation(coordinator(), 4100, "coordinator")
    assert facts["non_loopback_connect_results"] == {"192.168.64.3": False, "2001:db8::5": False}


def test_audit_tool_failure_during_isolation_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> list[netaudit.Listener]:
        raise netaudit.AuditError("ss -t exited 1")

    monkeypatch.setattr(dmtcp, "current_listeners", broken)
    with pytest.raises(BackendError) as err:
        dmtcp.DmtcpBackend._verify_isolation(coordinator(), 4100, "coordinator")
    assert err.value.code is ErrorCode.PERMISSION_DENIED and "audit failed" in err.value.message


# ------------------------------------------------------------ incomplete images


IMAGE = "ckpt_app_40000-1-1.dmtcp"
SCRIPT = "dmtcp_restart_script_x.sh"


@pytest.mark.parametrize(("files", "reason"), [
    ({IMAGE + ".temp": b"partial"}, "temporary checkpoint file"),
    ({IMAGE: b""}, "size 0"),
    ({IMAGE: b"img"}, "has not written the restart script"),
    ({IMAGE: b"img", SCRIPT: b"port=9999\n" + IMAGE.encode()}, "coordinator port 4100"),
    ({IMAGE: b"img", SCRIPT: b"port=4100\n"}, "does not reference image"),
    ({IMAGE: b"a", "ckpt_b_1.dmtcp": b"b", SCRIPT: b"4100 " + IMAGE.encode()}, "expected 1"),
])
def test_incomplete_checkpoints_are_never_complete(tmp_path: Path, files: dict[str, bytes],
                                                   reason: str) -> None:
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    ev = checkpoint_evidence(tmp_path, 4100, 0, 1)
    assert not ev.complete and reason in ev.reason


def test_complete_checkpoint_evidence(tmp_path: Path) -> None:
    (tmp_path / IMAGE).write_bytes(b"img")
    (tmp_path / SCRIPT).write_bytes(b"coord_port=4100\nckpt='" + IMAGE.encode() + b"'\n")
    assert checkpoint_evidence(tmp_path, 4100, 0, 1).complete


# ---------------------------------------------------------- GUI and cleanup


@pytest.mark.parametrize("lib", ["/usr/lib/aarch64-linux-gnu/libX11.so.6",
                                 "/usr/lib/aarch64-linux-gnu/libwayland-client.so.0",
                                 "/usr/lib/aarch64-linux-gnu/libgtk-3.so.0",
                                 "/usr/lib/aarch64-linux-gnu/libQt6Gui.so.6"])
def test_gui_toolkits_are_refused(lib: str) -> None:
    facts = RuntimeFacts(10, frozenset({10}), [], frozenset({lib}), "/opt/app/bin/app")
    assert {f.code for f in runtime_findings(facts, range(820, 860))} == {ReasonCode.GUI_TOOLKIT}


def test_cleanup_is_safe_to_repeat() -> None:
    b = dmtcp.DmtcpBackend(tools=None)
    unknown = BackendSession("never-started", 1, 1, 1, [])
    assert b.cleanup(None, None) == [] and b.cleanup(unknown, None) == []
    assert b.cleanup(unknown, None) == [] and b.close_all() == []
    state = proc.SessionState("/x", None, [])
    assert proc.cleanup_recorded(state) == ([], []) == proc.cleanup_recorded(state)


def test_exposure_detail_names_owner_inode_and_session(monkeypatch: pytest.MonkeyPatch) -> None:
    class Tracker:
        def records(self) -> list[dict[str, Any]]:
            return [{"name": "dmtcp-restart", "pid": 4242, "start_ticks": 9, "uid": 1000,
                     "boot_id": "b"}]

    class Guard:
        notes = [{"proto": "udp", "local": "0.0.0.0:50304", "uid": 991}]

    live = type("Live", (), {"tracker": Tracker(), "guard": Guard()})()
    exposed = [netaudit.Listener("udp", "0.0.0.0:50304", (), 1000, 44123)]
    detail = dmtcp.DmtcpBackend._exposure_detail(live, "restore", exposed, {4242})
    assert detail["listeners"][0] == {"proto": "udp", "local": "0.0.0.0:50304", "pids": (),
                                      "uid": 1000, "inode": 44123}
    assert detail["session_pids"] == [4242] and detail["session_processes"][0]["pid"] == 4242
    assert detail["step"] == "restore" and detail["observed_utc"] and detail["host_sockets_seen"]
