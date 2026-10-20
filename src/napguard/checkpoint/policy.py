"""When a DMTCP coordinator may run (ADR 0006 §1-§2). Default: never.

DMTCP 4.2.0's coordinator has no authentication. Loopback confinement keeps remote
hosts out, but every local user can reach 127.0.0.1. ADR 0006 therefore allows a
coordinator only for Stage 03a developer use on a single-user host, by an explicit
per-invocation acknowledgement. There is no production mode: a later ADR must remove
the local-user exposure before the daemon (Stage 06) may start coordinators.

This module does not read or change the Stage 02 harness flag
`DMTCP_LOOPBACK_ONLY_COORDINATOR_VERIFIED` (which stays False).
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from napguard.core.proc import filtered_env, other_user_processes

from .base import BackendError, ErrorCode

SINGLE_USER_FLAG = "--i-accept-single-user-coordinator"
DENIED = (
    "refusing to start a DMTCP coordinator: it has no authentication, so any local user "
    f"could command it. Stage 03a allows it only with {SINGLE_USER_FLAG} on a single-user "
    "host (ADR 0006)")


def parse_loginctl_sessions(output: str) -> list[tuple[str, int]]:
    """(session, uid) from `loginctl list-sessions --no-legend` (SESSION UID USER ...)."""
    found = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            found.append((parts[0], int(parts[1])))
    return found


def logind_sessions() -> list[tuple[str, int]]:
    tool = shutil.which("loginctl", path="/usr/bin:/bin")
    if tool is None:
        raise RuntimeError("loginctl not found")
    done = subprocess.run([tool, "list-sessions", "--no-legend", "--no-pager"],
                          capture_output=True, text=True, timeout=10, env=filtered_env(),
                          stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        raise RuntimeError(f"loginctl exited {done.returncode}: {done.stderr.strip()}")
    return parse_loginctl_sessions(done.stdout)


def single_user_host_findings(
        own_uid: int,
        sessions: Callable[[], list[tuple[str, int]]] = logind_sessions,
        processes: Callable[[], list[int]] | None = None) -> list[str]:
    """Why this host does not look single-user. Empty means no other user was found.

    Only regular users count (uid >= 1000, not 65534 nobody); a display manager's
    greeter session (for example gdm, a system account) does not block.
    A heuristic, not a guarantee (ADR 0006 §2): system accounts (uid < 1000) and users
    who appear after the check are not excluded. Fails closed if logind cannot be read.
    """
    findings = []
    try:
        others = sorted({uid for _, uid in sessions()
                         if uid != own_uid and uid >= 1000 and uid != 65534})
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        findings.append(f"cannot list logind sessions ({exc}); refusing")
    else:
        if others:
            findings.append(f"logind sessions of other users: uids {others}")
    if processes is not None:
        uids = sorted(set(processes()))
    else:
        uids = sorted({p.ident.uid for p in other_user_processes(own_uid)})
    if uids:
        findings.append(f"processes of other regular users: uids {uids}")
    return findings


@dataclass(frozen=True)
class CoordinatorPolicy:
    single_user_acknowledged: bool = False

    @classmethod
    def deny(cls) -> CoordinatorPolicy:
        return cls()

    @classmethod
    def experimental_single_user(cls, acknowledged: bool) -> CoordinatorPolicy:
        """The only permissive policy: Stage 03a developer mode (ADR 0006)."""
        return cls(single_user_acknowledged=bool(acknowledged))

    def authorize(self, own_uid: int,
                  host_findings: Callable[[int], list[str]] = single_user_host_findings) -> None:
        """Raise PERMISSION_DENIED unless a coordinator may start now. Starts nothing."""
        if not self.single_user_acknowledged:
            raise BackendError(ErrorCode.PERMISSION_DENIED, DENIED)
        findings = host_findings(own_uid)
        if findings:
            raise BackendError(ErrorCode.PERMISSION_DENIED,
                               "refusing to start a DMTCP coordinator: this host does not "
                               "look single-user: " + "; ".join(findings),
                               detail={"findings": findings})
