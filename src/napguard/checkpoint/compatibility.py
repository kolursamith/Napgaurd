"""Workload compatibility assessment and fail-closed rules (ADR 0006 §3 and §5).

Every Stage 02 negative finding is an explicit rule with a reason code (CMP-006), and
every reason code has a message (FR-034). A workload is `VERIFIED` only for NapGuard's
own reference workloads on the Stage 02-verified platform (CMP-002); anything else that
passes every rule is `UNKNOWN` (CMP-003), never "supported".
"""

from __future__ import annotations

import os
import re
import stat
import struct
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class Compatibility(StrEnum):
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"
    CANDIDATE = "CANDIDATE"
    VERIFIED = "VERIFIED"


class ReasonCode(StrEnum):
    KNOWN_RESTORE_FAILURE = "KNOWN_RESTORE_FAILURE"
    SOCKETS_HELD = "SOCKETS_HELD"
    MULTI_PROCESS = "MULTI_PROCESS"
    DEVICE_STATE = "DEVICE_STATE"
    GUI_TOOLKIT = "GUI_TOOLKIT"
    STATIC_BINARY = "STATIC_BINARY"
    SETUID = "SETUID"
    NOT_ELF = "NOT_ELF"
    NOT_EXECUTABLE = "NOT_EXECUTABLE"
    ARCH_MISMATCH = "ARCH_MISMATCH"
    UNVERIFIED_PLATFORM = "UNVERIFIED_PLATFORM"
    UNANALYSED_VERSION = "UNANALYSED_VERSION"


MESSAGES: dict[ReasonCode, str] = {
    ReasonCode.KNOWN_RESTORE_FAILURE: (
        "Python interpreters failed to restore under DMTCP 4.2.0 on aarch64 in Stage 02 "
        "(2 of 2 restores; SIGSEGV). Python workloads are refused."),
    ReasonCode.SOCKETS_HELD: (
        "The workload holds sockets. Restoring them would make DMTCP 4.2.0 open a listener "
        "on all interfaces (I-1c); network connections are not restored (LIM-003)."),
    ReasonCode.MULTI_PROCESS: (
        "The workload has more than one process. Multi-process restore and cleanup are "
        "not reviewed for Stage 03a."),
    ReasonCode.DEVICE_STATE: (
        "The workload has a device open. Device and GPU state is not restored (LIM-004)."),
    ReasonCode.GUI_TOOLKIT: (
        "The workload uses a GUI toolkit or display-server library. GUI programs keep "
        "display-server connections and are not supported (EXC-001)."),
    ReasonCode.STATIC_BINARY: (
        "The executable is statically linked; DMTCP needs a dynamically linked program."),
    ReasonCode.SETUID: (
        "The executable is setuid or setgid. NapGuard does not manage privileged programs."),
    ReasonCode.NOT_ELF: (
        "The executable is not an ELF program (scripts must be assessed via their "
        "interpreter)."),
    ReasonCode.NOT_EXECUTABLE: "The path is not an executable regular file.",
    ReasonCode.ARCH_MISMATCH: "The executable is built for a different CPU architecture.",
    ReasonCode.UNVERIFIED_PLATFORM: (
        "This OS, architecture, or backend version has no recorded passing evidence; the "
        "result can be UNKNOWN at best."),
    ReasonCode.UNANALYSED_VERSION: (
        "The DMTCP version is not 4.2.0; its coordinator isolation has not been analysed."),
}

# Reasons that make a workload UNSUPPORTED. UNVERIFIED_PLATFORM only caps the level.
REFUSING = frozenset(set(ReasonCode) - {ReasonCode.UNVERIFIED_PLATFORM})

# Stage 02 evidence (DMTCP 4.2.0, Ubuntu 24.04 aarch64; Stage 02 report section AB).
VERIFIED_CLASSES = ("c-single", "c-threads", "c-file-static", "c-file-grow")
VERIFIED_PLATFORM = {"backend": "dmtcp", "version": "4.2.0", "arch": "aarch64",
                     "os_id": "ubuntu", "os_version_id": "24.04"}

_PYTHON_NAME = re.compile(r"^(python|pypy)\d*(\.\d+)*[a-z]*$")
_LIBPYTHON = re.compile(r"^libpython\d")
_GUI_LIBS = re.compile(r"^(libX11|libxcb|libwayland-client|libgtk-|libgdk-|libQt\dGui|libSDL2)")
ELF_MACHINES = {"aarch64": 183, "x86_64": 62}
_ALLOWED_DEVICES = ("/dev/null", "/dev/zero", "/dev/full", "/dev/random", "/dev/urandom",
                    "/dev/tty", "/dev/ptmx")
_ALLOWED_DEVICE_PREFIXES = ("/dev/pts/", "/dev/shm/")


@dataclass(frozen=True)
class Finding:
    code: ReasonCode
    detail: str

    def to_json(self) -> dict[str, str]:
        return {"code": str(self.code), "message": MESSAGES[self.code], "detail": self.detail}


@dataclass(frozen=True)
class CompatibilityReport:
    level: Compatibility
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def refused(self) -> bool:
        return self.level is Compatibility.UNSUPPORTED

    def to_json(self) -> dict[str, object]:
        return {"level": str(self.level), "findings": [f.to_json() for f in self.findings],
                "notes": self.notes}


def decide(findings: Iterable[Finding], *, reference_class: str | None,
           platform_verified: bool) -> CompatibilityReport:
    found = list(findings)
    if any(f.code in REFUSING for f in found):
        return CompatibilityReport(Compatibility.UNSUPPORTED, found)
    if reference_class in VERIFIED_CLASSES and platform_verified:
        return CompatibilityReport(Compatibility.VERIFIED, found,
                                   [f"NapGuard reference workload, class {reference_class}"])
    return CompatibilityReport(Compatibility.UNKNOWN, found, [
        "No recorded passing evidence for this workload: it becomes VERIFIED only after a real "
        "checkpoint and restore of this workload on this backend, version, and architecture."])


# ----------------------------------------------------------------- ELF facts


@dataclass(frozen=True)
class ElfInfo:
    machine: int
    has_interp: bool


def parse_elf(header: bytes) -> ElfInfo | None:
    """Machine and PT_INTERP presence from the start of an ELF file; None if not ELF."""
    if len(header) < 64 or header[:4] != b"\x7fELF" or header[4] not in (1, 2):
        return None
    endian = "<" if header[5] == 1 else ">"
    is64 = header[4] == 2
    machine = struct.unpack_from(endian + "H", header, 18)[0]
    if is64:
        phoff = struct.unpack_from(endian + "Q", header, 32)[0]
        phentsize, phnum = struct.unpack_from(endian + "HH", header, 54)
    else:
        phoff = struct.unpack_from(endian + "I", header, 28)[0]
        phentsize, phnum = struct.unpack_from(endian + "HH", header, 42)
    has_interp = False
    for i in range(min(phnum, 256)):
        offset = phoff + i * phentsize
        if offset + 4 > len(header):
            break
        if struct.unpack_from(endian + "I", header, offset)[0] == 3:  # PT_INTERP
            has_interp = True
            break
    return ElfInfo(machine, has_interp)


def is_python_name(path: str) -> bool:
    return bool(_PYTHON_NAME.match(os.path.basename(path)))


def static_findings(executable: Path, arch: str) -> list[Finding]:
    """Rules that need only the executable file (no process is started)."""
    try:
        real = executable.resolve(strict=True)
        st = real.stat()
    except OSError as exc:
        return [Finding(ReasonCode.NOT_EXECUTABLE, f"{executable}: {exc.strerror}")]
    if not stat.S_ISREG(st.st_mode) or not os.access(real, os.X_OK):
        return [Finding(ReasonCode.NOT_EXECUTABLE, str(real))]
    found = []
    if st.st_mode & (stat.S_ISUID | stat.S_ISGID):
        found.append(Finding(ReasonCode.SETUID, f"{real} mode {st.st_mode & 0o7777:o}"))
    if is_python_name(str(real)):
        found.append(Finding(ReasonCode.KNOWN_RESTORE_FAILURE, f"interpreter {real}"))
    with real.open("rb") as fh:
        head = fh.read(4096)
    elf = parse_elf(head)
    if elf is None:
        if head.startswith(b"#!"):
            interpreter = head[2:].split(b"\n", 1)[0].strip().split()
            names = [p.decode(errors="replace") for p in interpreter[:2]]
            if any(is_python_name(n) for n in names):
                found.append(Finding(ReasonCode.KNOWN_RESTORE_FAILURE,
                                     f"script interpreter {' '.join(names)}"))
        found.append(Finding(ReasonCode.NOT_ELF, str(real)))
        return found
    expected = ELF_MACHINES.get(arch)
    if expected is not None and elf.machine != expected:
        found.append(Finding(ReasonCode.ARCH_MISMATCH, f"ELF machine {elf.machine}, host {arch}"))
    if not elf.has_interp:
        found.append(Finding(ReasonCode.STATIC_BINARY, f"{real} has no program interpreter"))
    return found


# --------------------------------------------------------------- runtime facts


@dataclass(frozen=True)
class RuntimeFacts:
    pid: int
    group_pids: frozenset[int]
    fds: list[tuple[int, str]]
    mapped: frozenset[str]
    exe: str


def runtime_findings(facts: RuntimeFacts, protected_fds: range) -> list[Finding]:
    """Rules checked on the live workload before every checkpoint."""
    found = []
    if facts.group_pids != {facts.pid}:
        found.append(Finding(ReasonCode.MULTI_PROCESS,
                             f"process group members {sorted(facts.group_pids)}"))
    own = [(fd, link) for fd, link in facts.fds if fd not in protected_fds]
    sockets = [f"{fd}:{link}" for fd, link in own if link.startswith("socket:")]
    if sockets:
        found.append(Finding(ReasonCode.SOCKETS_HELD, ", ".join(sockets[:10])))
    devices = [f"{fd}:{link}" for fd, link in own if link.startswith("/dev/")
               and link not in _ALLOWED_DEVICES and not link.startswith(_ALLOWED_DEVICE_PREFIXES)]
    if devices:
        found.append(Finding(ReasonCode.DEVICE_STATE, ", ".join(devices[:10])))
    gui = sorted(p for p in facts.mapped if _GUI_LIBS.match(os.path.basename(p)))
    if gui:
        found.append(Finding(ReasonCode.GUI_TOOLKIT, ", ".join(gui[:3])))
    python = sorted(p for p in facts.mapped if _LIBPYTHON.match(os.path.basename(p)))
    if is_python_name(facts.exe) or python:
        found.append(Finding(ReasonCode.KNOWN_RESTORE_FAILURE,
                             f"exe {facts.exe}" + (f"; maps {python[:3]}" if python else "")))
    return found


def collect_runtime_facts(pid: int, pgid: int,
                          members: Callable[[int], Iterable[int]] | None = None) -> RuntimeFacts:
    from napguard.core import proc  # Linux-only facts; imported lazily

    group = frozenset(members(pgid) if members is not None
                      else (m.ident.pid for m in proc.running_group_members(pgid)))
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        exe = "?"
    return RuntimeFacts(pid, group, proc.fd_links(pid), frozenset(proc.mapped_paths(pid)), exe)
