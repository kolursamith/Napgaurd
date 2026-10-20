"""Stage 03a: fail-closed compatibility rules (CMP-006) and network audits."""

from __future__ import annotations

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
