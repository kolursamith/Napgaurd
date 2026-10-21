"""Stage 03a: fail-closed compatibility rules (CMP-006) and network audits."""

from __future__ import annotations

import os
import platform
import struct
from pathlib import Path

import pytest

from napguard.checkpoint import compatibility as compat
from napguard.checkpoint.compatibility import Compatibility, ReasonCode
from napguard.core import netaudit

linux_only = pytest.mark.skipif(platform.system() != "Linux", reason="needs Linux")
PROTECTED = range(820, 860)


def elf_bytes(machine: int = 183, interp: bool = True) -> bytes:
    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4], header[5], header[6] = 2, 1, 1  # 64-bit, little endian, version 1
    struct.pack_into("<HHI", header, 16, 3, machine, 1)
    struct.pack_into("<Q", header, 32, 64)  # program headers right after the header
    struct.pack_into("<HH", header, 54, 56, 1)
    ph = bytearray(56)
    struct.pack_into("<I", ph, 0, 3 if interp else 1)  # PT_INTERP or PT_LOAD
    return bytes(header + ph)


def program(tmp_path: Path, name: str = "app", data: bytes | None = None,
            mode: int = 0o755) -> Path:
    path = tmp_path / name
    path.write_bytes(elf_bytes() if data is None else data)
    path.chmod(mode)
    return path


def codes(findings: list[compat.Finding]) -> set[ReasonCode]:
    return {f.code for f in findings}


def test_dynamic_elf_for_this_arch_has_no_findings(tmp_path: Path) -> None:
    assert compat.static_findings(program(tmp_path), "aarch64") == []


def test_static_wrong_arch_setuid_and_non_elf_are_refused(tmp_path: Path) -> None:
    static = program(tmp_path, "static", elf_bytes(interp=False))
    assert codes(compat.static_findings(static, "aarch64")) == {ReasonCode.STATIC_BINARY}
    x86 = program(tmp_path, "x86", elf_bytes(machine=62))
    assert ReasonCode.ARCH_MISMATCH in codes(compat.static_findings(x86, "aarch64"))
    suid = program(tmp_path, "suid", mode=0o4755)
    assert ReasonCode.SETUID in codes(compat.static_findings(suid, "aarch64"))
    script = program(tmp_path, "run.sh", b"#!/bin/sh\necho hi\n")
    assert codes(compat.static_findings(script, "aarch64")) == {ReasonCode.NOT_ELF}
    plain = program(tmp_path, "data", mode=0o644)
    assert codes(compat.static_findings(plain, "aarch64")) == {ReasonCode.NOT_EXECUTABLE}


def test_python_interpreters_and_scripts_are_known_restore_failures(tmp_path: Path) -> None:
    for name in ("python3", "python3.12", "pypy3"):
        assert ReasonCode.KNOWN_RESTORE_FAILURE in codes(
            compat.static_findings(program(tmp_path, name), "aarch64"))
    script = program(tmp_path, "tool", b"#!/usr/bin/env python3\nprint(1)\n")
    assert ReasonCode.KNOWN_RESTORE_FAILURE in codes(compat.static_findings(script, "aarch64"))
    assert not compat.is_python_name("pythonic-app")


def facts(**over: object) -> compat.RuntimeFacts:
    values: dict[str, object] = {"pid": 10, "group_pids": frozenset({10}),
                                 "fds": [(0, "/dev/null"), (1, "/run/x.log"), (2, "/dev/pts/1"),
                                         (821, "socket:[9]"), (830, "/dev/shm/dmtcp")],
                                 "mapped": frozenset({"/usr/lib/libc.so.6"}),
                                 "exe": "/home/u/bin/ngcounter"}
    values.update(over)
    return compat.RuntimeFacts(**values)  # type: ignore[arg-type]


def test_runtime_rules_ignore_dmtcp_protected_descriptors() -> None:
    assert compat.runtime_findings(facts(), PROTECTED) == []


@pytest.mark.parametrize(("over", "code"), [
    ({"group_pids": frozenset({10, 11})}, ReasonCode.MULTI_PROCESS),
    ({"fds": [(3, "socket:[77]")]}, ReasonCode.SOCKETS_HELD),
    ({"fds": [(4, "/dev/dri/renderD128")]}, ReasonCode.DEVICE_STATE),
    ({"mapped": frozenset({"/usr/lib/libpython3.12.so.1.0"})}, ReasonCode.KNOWN_RESTORE_FAILURE),
    ({"exe": "/usr/bin/python3.12"}, ReasonCode.KNOWN_RESTORE_FAILURE),
])
def test_runtime_rules_refuse_stage02_negative_findings(over: dict[str, object],
                                                        code: ReasonCode) -> None:
    assert code in codes(compat.runtime_findings(facts(**over), PROTECTED))


def test_levels_never_claim_support_without_evidence() -> None:
    refuse = [compat.Finding(ReasonCode.SOCKETS_HELD, "3")]
    assert compat.decide(refuse, reference_class="c-single", platform_verified=True).level \
        is Compatibility.UNSUPPORTED
    assert compat.decide([], reference_class=None, platform_verified=True).level \
        is Compatibility.UNKNOWN
    assert compat.decide([], reference_class="c-single", platform_verified=False).level \
        is Compatibility.UNKNOWN
    assert compat.decide([], reference_class="py", platform_verified=True).level \
        is Compatibility.UNKNOWN
    assert compat.decide([], reference_class="c-threads", platform_verified=True).level \
        is Compatibility.VERIFIED
    assert set(compat.MESSAGES) == set(ReasonCode)  # FR-034: every code has a message


# ------------------------------------------------------------- network audit


def test_parse_ss_and_guard_flags_owned_and_new_listeners() -> None:
    out = ("LISTEN 0 128 127.0.0.1:41000 0.0.0.0:* users:((\"dmtcp_coordinat\",pid=50,fd=821))\n"
           "LISTEN 0 128 0.0.0.0:22 0.0.0.0:* \n")
    parsed = netaudit.parse_ss_listeners(out)
    assert parsed[0].loopback and parsed[0].pids == (50,) and not parsed[1].loopback
    snapshots = [parsed, [*parsed, netaudit.Listener("tcp", "0.0.0.0:7779", ())],
                 [*parsed, netaudit.Listener("tcp", "[::]:41000", (50,))]]
    guard = netaudit.ListenerGuard(lambda: snapshots.pop(0))
    assert guard.baseline == {("tcp", "0.0.0.0:22")}  # pre-existing: not ours to judge
    assert [x.local for x in guard.violations({99})] == ["0.0.0.0:7779"]  # new, any owner
    assert [x.local for x in guard.violations({50})] == ["[::]:41000"]  # owned by the session


def test_missing_audit_tool_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("napguard.core.netaudit.shutil.which", lambda *a, **k: None)
    with pytest.raises(netaudit.AuditError):
        netaudit.current_listeners()
    with pytest.raises(netaudit.AuditError):
        netaudit.non_loopback_ipv4_addresses()


@linux_only
def test_wildcard_bind_is_refused_next_to_a_loopback_listener() -> None:
    listener = netaudit.loopback_listener()
    try:
        assert netaudit.wildcard_bind_refused(listener.getsockname()[1]) is True
    finally:
        listener.close()


# ------------------- socket ownership: host sockets vs session exposure (Ubuntu e81c010)

OWN = os.getuid()
SS_E = (
    # a system resolver's transient unconnected UDP socket (the Ubuntu observation)
    "UNCONN 0 0 0.0.0.0:50304 0.0.0.0:* uid:991 ino:44123 sk:1f "
    "cgroup:/system.slice/systemd-resolved.service <->\n"
    # root-owned: ss omits uid:0
    "UNCONN 0 0 0.0.0.0:68 0.0.0.0:* ino:2222 sk:2 <->\n"
    # our coordinator
    f'LISTEN 0 128 127.0.0.1:41000 0.0.0.0:* users:(("dmtcp_coordinat",pid=50,fd=821)) '
    f"uid:{OWN} ino:9 sk:3\n"
    # a hostile own process name trying to look root-owned
    f'LISTEN 0 1 0.0.0.0:7000 0.0.0.0:* users:(("x uid:0 ino:1",pid=60,fd=3)) '
    f"uid:{OWN} ino:77 sk:4\n")


def test_ss_details_give_the_kernel_owner_and_resist_spoofed_names() -> None:
    parsed = {x.local: x for x in netaudit.parse_ss_listeners(SS_E, "udp")}
    assert (parsed["0.0.0.0:50304"].uid, parsed["0.0.0.0:50304"].inode) == (991, 44123)
    assert parsed["0.0.0.0:50304"].pids == ()
    assert parsed["0.0.0.0:68"].uid == 0  # uid omitted by ss means root
    assert (parsed["127.0.0.1:41000"].pids, parsed["127.0.0.1:41000"].uid) == ((50,), OWN)
    spoof = parsed["0.0.0.0:7000"]
    assert (spoof.uid, spoof.inode, spoof.pids) == (OWN, 77, (60,))  # the last fields win
    assert netaudit.socket_owner("LISTEN 0 1 0.0.0.0:1 0.0.0.0:*") == (None, None)


def L(local: str, uid: int | None, pids: tuple[int, ...] = (), proto: str = "udp",
      inode: int | None = 5) -> netaudit.Listener:
    return netaudit.Listener(proto, local, pids, uid, inode)


def guard_with(*later: list[netaudit.Listener]) -> netaudit.ListenerGuard:
    snapshots = [[], *later]
    return netaudit.ListenerGuard(lambda: snapshots.pop(0))


def test_new_system_account_udp_socket_is_noted_not_an_exposure() -> None:
    resolver = L("0.0.0.0:50304", 991, inode=44123)
    guard = guard_with([resolver], [resolver])
    assert guard.violations({4242}, "restore") == []
    assert guard.violations({4242}, "restore (waiting)") == []
    assert len(guard.notes) == 1  # recorded once, with its owner and inode
    note = guard.notes[0]
    assert (note["proto"], note["local"], note["uid"], note["inode"], note["step"]) == (
        "udp", "0.0.0.0:50304", 991, 44123, "restore")
    assert guard_with([L("0.0.0.0:68", 0)]).violations(set()) == []  # root
    assert guard_with([L("0.0.0.0:5", 65534)]).violations(set()) == []  # nobody


@pytest.mark.parametrize("listener", [
    L("0.0.0.0:50304", OWN),  # this user's socket outside the session (a stray restore)
    L("0.0.0.0:50304", None, inode=None),  # owner unknown: fail closed
    L("0.0.0.0:50304", 1001),  # another regular user
    L("0.0.0.0:50304", 991, pids=(77,)),  # attributed to a process we can see
    L("[::]:50304", OWN),  # IPv6 wildcard
    L("0.0.0.0:41000", OWN, proto="tcp"),  # a TCP listener (I-1c restore listener)
])
def test_other_new_listeners_remain_exposures(listener: netaudit.Listener) -> None:
    assert guard_with([listener]).violations(set()) == [listener]


def test_session_owned_wildcard_listener_is_always_an_exposure() -> None:
    # Even with a system uid reported, a non-loopback listener of a session pid fails.
    coord = L("0.0.0.0:41000", 0, pids=(4242,), proto="tcp")
    assert guard_with([coord]).violations({4242}) == [coord]


def test_host_socket_notes_are_bounded() -> None:
    many = [L(f"0.0.0.0:{40000 + i}", 991, inode=i) for i in range(200)]
    guard = guard_with(many)
    assert guard.violations(set()) == [] and len(guard.notes) == netaudit.MAX_NOTES
