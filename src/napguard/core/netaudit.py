"""Network exposure audits for backend helper processes (ADR 0006 §1-§2).

Reviewed port of the Stage 02 harness audits (`experiments/stage02/ngexp/runner.py`),
verified on Ubuntu 24.04 aarch64. Every audit is fail-closed: if a tool is missing or
fails, `AuditError` is raised and the caller must stop what it was doing.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .proc import filtered_env


class AuditError(RuntimeError):
    """The audit could not run; nothing may be assumed about exposure."""


@dataclass(frozen=True)
class Listener:
    proto: str
    local: str
    pids: tuple[int, ...]
    uid: int | None = None  # socket owner (`ss -e`); None if unknown
    inode: int | None = None

    @property
    def loopback(self) -> bool:
        return self.local.startswith(("127.", "[::1]:", "::1:"))

    @property
    def port(self) -> int:
        return int(self.local.rsplit(":", 1)[1])


def socket_owner(line: str) -> tuple[int | None, int | None]:
    """(uid, inode) from the `ss -e` details at the END of a line.

    ss prints the kernel's ` uid:N ino:N sk:...` after the `users:((...))` block, and
    omits `uid:` when it is 0 (root). Only the LAST matches are used: a process name in the
    users block (which only our own processes can contribute) cannot spoof them.
    """
    inos = list(re.finditer(r" ino:(\d+)", line))
    if not inos:
        return None, None
    inode = int(inos[-1].group(1))
    uids = [m for m in re.finditer(r" uid:(\d+)", line) if m.start() < inos[-1].start()]
    return (int(uids[-1].group(1)) if uids else 0), inode


def parse_ss_listeners(output: str, proto: str = "tcp") -> list[Listener]:
    """Parse `ss -H -l -n -p [-e]` lines:
    State Recv-Q Send-Q Local Peer [users:((...,pid=N,...))] [uid:N ino:N sk:...]."""
    found = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        # Extra (even spoofed) pid= entries can only make the audit stricter.
        pids = tuple(int(p) for p in re.findall(r"pid=(\d+)", line))
        uid, inode = socket_owner(line)
        found.append(Listener(proto, parts[3], pids, uid, inode))
    return found


def _tool(name: str) -> str:
    path = shutil.which(name, path="/usr/sbin:/usr/bin:/sbin:/bin")
    if path is None:
        raise AuditError(f"`{name}` not found: cannot audit network exposure")
    return path


def _run(argv: Sequence[str]) -> str:
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=10,
                              env=filtered_env(), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AuditError(f"{argv[0]} failed: {exc}") from exc
    if done.returncode != 0:
        raise AuditError(f"{' '.join(argv)} exited {done.returncode}: {done.stderr.strip()}")
    return done.stdout


def current_listeners() -> list[Listener]:
    """All TCP and UDP listeners; owners are visible for this user's own processes."""
    ss = _tool("ss")
    found: list[Listener] = []
    for proto, flag in (("tcp", "-t"), ("udp", "-u")):
        found.extend(parse_ss_listeners(_run([ss, "-H", "-l", "-n", "-p", "-e", flag]), proto))
    return found


def non_loopback_ipv4_addresses() -> list[str]:
    out = _run([_tool("ip"), "-o", "-4", "addr", "show"])
    return [a for a in re.findall(r"\binet (\d+\.\d+\.\d+\.\d+)/", out) if not a.startswith("127.")]


def non_loopback_ipv6_addresses() -> list[str]:
    """Global-scope IPv6 addresses (link-local ones need a scope id and are not probed)."""
    out = _run([_tool("ip"), "-o", "-6", "addr", "show", "scope", "global"])
    return re.findall(r"\binet6 ([0-9a-fA-F:]+)/", out)


def listener_violations(listeners: Sequence[Listener], pids: set[int]) -> list[Listener]:
    """Non-loopback listeners owned by any of `pids`."""
    return [lst for lst in listeners if not lst.loopback and set(lst.pids) & pids]


def new_non_loopback(listeners: Sequence[Listener],
                     baseline: set[tuple[str, str]]) -> list[Listener]:
    """Non-loopback listeners (any owner) that were not present in the baseline."""
    return [lst for lst in listeners if not lst.loopback and (lst.proto, lst.local) not in baseline]


def tcp_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def loopback_listener() -> socket.socket:
    """A TCP listener bound to 127.0.0.1 on a kernel-chosen port (no SO_REUSEPORT)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    return sock


def wildcard_bind_refused(port: int) -> bool:
    """True if binding 0.0.0.0:port fails with EADDRINUSE (the fail-closed property)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("0.0.0.0", port))
    except OSError as exc:
        return exc.errno == errno.EADDRINUSE
    finally:
        probe.close()
    return False


SYSTEM_UID_LIMIT = 1000  # below this: root and system service accounts
NOBODY_UID = 65534
MAX_NOTES = 50


def system_owned(lst: Listener) -> bool:
    """True only for a socket PROVABLY owned by root or a system account (ADR 0006 §1.4)."""
    return (lst.uid is not None and not lst.pids
            and (lst.uid < SYSTEM_UID_LIMIT or lst.uid == NOBODY_UID))


class ListenerGuard:
    """Baseline-diff plus ownership audit for one backend session.

    The baseline is taken BEFORE the session starts anything. `violations(pids)` returns
    (a) every non-loopback listener owned by `pids`, and (b) every NEW non-loopback listener,
    unless the kernel reports it as owned by root or a system account with no process of
    ours attached. Such host sockets (for example a resolver's or time-sync client's
    unconnected UDP socket on 0.0.0.0) cannot belong to this session: every session
    process runs as this user, never setuid. They are recorded in `notes`, not ignored
    silently. A new listener of THIS user, of another regular user, or of unknown owner
    is always a violation, so a restored process outside the session is still caught.
    """

    def __init__(self, listeners: Callable[[], list[Listener]] = current_listeners) -> None:
        self._listeners = listeners
        self.baseline = {(lst.proto, lst.local) for lst in listeners() if not lst.loopback}
        self.notes: list[dict[str, Any]] = []

    def violations(self, pids: set[int], step: str = "audit") -> list[Listener]:
        current = self._listeners()
        exposed = listener_violations(current, pids)
        for lst in new_non_loopback(current, self.baseline):
            if lst in exposed:
                continue
            if system_owned(lst) and lst.uid != os.getuid():
                self._note(lst, step)
                continue
            exposed.append(lst)
        return exposed

    def _note(self, lst: Listener, step: str) -> None:
        key = (lst.proto, lst.local, lst.inode)
        if len(self.notes) >= MAX_NOTES or any(
                (n["proto"], n["local"], n["inode"]) == key for n in self.notes):
            return
        self.notes.append({"proto": lst.proto, "local": lst.local, "uid": lst.uid,
                           "inode": lst.inode, "step": step,
                           "first_seen_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                           "verdict": "host socket of a system account; not NapGuard's"})
