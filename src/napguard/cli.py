"""`napguard-dev`: the Stage 03a developer CLI (ADR 0006; developer mode only).

    napguard-dev backend probe [--json]
    napguard-dev assess [--json] -- <executable> [args...]
    napguard-dev selftest --i-accept-single-user-coordinator [--class c-single|c-threads]
    napguard-dev cleanup --session-dir DIR

`backend assess`, `backend selftest`, and `backend cleanup` are the same commands.
Without `--i-accept-single-user-coordinator`, no DMTCP coordinator is ever started.
Exit status: 0 PASS/OK, 1 FAIL (or UNSUPPORTED for assess), 2 BLOCKED or refused.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from napguard.checkpoint import artifacts
from napguard.checkpoint.base import BackendError, WorkloadSpec
from napguard.checkpoint.compatibility import Compatibility
from napguard.checkpoint.criu import CriuBackend
from napguard.checkpoint.dmtcp import DmtcpBackend
from napguard.checkpoint.policy import SINGLE_USER_FLAG, CoordinatorPolicy
from napguard.core import proc
from napguard.selftest.run import CLASSES, SelftestOptions, run_selftest
from napguard.selftest.verify import Status

EXIT = {Status.PASS: 0, Status.FAIL: 1, Status.BLOCKED: 2}


def default_work_root() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "napguard-dev" / "selftest"


def _emit(data: object, as_json: bool, text: str) -> None:
    print(json.dumps(data, indent=2, sort_keys=True, default=str) if as_json else text)


def cmd_probe(args: argparse.Namespace) -> int:
    dmtcp = DmtcpBackend(prefix=args.dmtcp_prefix).probe()
    criu = CriuBackend(args.criu_bin).probe()
    lines = []
    for caps in (dmtcp, criu):
        lines.append(f"{caps.name}: available={caps.available} version={caps.version} "
                     f"verified={caps.verified} arch={caps.arch} "
                     f"os={caps.os_id} {caps.os_version_id}")
        lines += [f"  tool {n}: {p}" for n, p in caps.tool_paths.items()]
        lines.append(f"  self-check: {caps.self_check}")
        lines += [f"  note: {n}" for n in caps.notes]
    _emit({"dmtcp": asdict(dmtcp), "criu": asdict(criu)}, args.json, "\n".join(lines))
    return 0 if dmtcp.available else 2


def cmd_assess(args: argparse.Namespace) -> int:
    command = [c for c in args.command if c != "--"]
    if not command:
        print("assess: give the program to assess after `--`", file=sys.stderr)
        return 2
    exe = command[0] if "/" in command[0] else (shutil.which(command[0]) or command[0])
    spec = WorkloadSpec("assess", Path(exe), [exe, *command[1:]], Path.cwd(), proc.filtered_env())
    report = DmtcpBackend(prefix=args.dmtcp_prefix).assess(spec)
    lines = [f"{exe}: {report.level}"]
    lines += [f"  {f.code}: {f.detail}" for f in report.findings]
    lines += [f"  note: {n}" for n in report.notes]
    _emit(report.to_json(), args.json, "\n".join(lines))
    return 1 if report.level is Compatibility.UNSUPPORTED else 0


def cmd_selftest(args: argparse.Namespace) -> int:
    if not args.i_accept_single_user_coordinator:
        print(f"selftest: BLOCKED. It starts an unauthenticated DMTCP coordinator, which "
              f"Stage 03a allows only in developer mode on a single-user host (ADR 0006). "
              f"Re-run with {SINGLE_USER_FLAG} to accept that. No coordinator was started.",
              file=sys.stderr)
        return 2
    backend = DmtcpBackend(CoordinatorPolicy.experimental_single_user(True),
                           prefix=args.dmtcp_prefix)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    work_dir = args.work_root / f"{stamp}-{args.workload_class}-{secrets.token_hex(2)}"
    args.work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        result = run_selftest(backend, SelftestOptions(work_dir, args.workload_class))
    finally:
        backend.close_all()
    lines = [f"{c.status:<7}  {c.name} — {c.detail}" for c in result.checks]
    lines.append(f"\nSELFTEST VERDICT: {result.status}   evidence: {work_dir}")
    _emit(result.to_json(), args.json, "\n".join(lines))
    return EXIT[result.status]


def cmd_cleanup(args: argparse.Namespace) -> int:
    session_dir: Path = args.session_dir
    try:
        with artifacts.private_dir(session_dir):
            pass
        state = proc.SessionState.read(session_dir / "session.json")
    except (BackendError, OSError, ValueError, KeyError) as exc:
        print(f"cleanup: refused: {exc}", file=sys.stderr)
        return 2
    stopped, skipped = proc.cleanup_recorded(state)
    leaks = [f"pid {p.ident.pid}" for p in proc.leaked_processes(session_dir)]
    _emit({"stopped": stopped, "skipped": skipped, "leaks_reported_not_killed": leaks}, args.json,
          "\n".join([f"stopped: {s}" for s in stopped] + [f"skipped: {s}" for s in skipped]
                    + [f"still referencing the session (reported, not killed): {x}"
                       for x in leaks]))
    return 1 if leaks else 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="napguard-dev",
                                description="NapGuard Stage 03a developer CLI (developer mode)")
    top = p.add_subparsers(dest="command", required=True)
    backend = top.add_parser("backend", help="backend commands").add_subparsers(
        dest="backend_command", required=True)

    def add_assess(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        a = sub.add_parser("assess", help="static compatibility assessment (starts nothing)")
        a.add_argument("--json", action="store_true")
        a.add_argument("--dmtcp-prefix", type=Path)
        a.add_argument("command", nargs=argparse.REMAINDER)
        a.set_defaults(func=cmd_assess)

    def add_selftest(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        s = sub.add_parser("selftest", help="real checkpoint/restore of the reference workload")
        s.add_argument(SINGLE_USER_FLAG, dest="i_accept_single_user_coordinator",
                       action="store_true")
        s.add_argument("--class", dest="workload_class", choices=sorted(CLASSES),
                       default="c-single")
        s.add_argument("--work-root", type=Path, default=default_work_root())
        s.add_argument("--dmtcp-prefix", type=Path)
        s.add_argument("--json", action="store_true")
        s.set_defaults(func=cmd_selftest)

    def add_cleanup(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        c = sub.add_parser("cleanup", help="stop identity-matched processes of a crashed session")
        c.add_argument("--session-dir", type=Path, required=True)
        c.add_argument("--json", action="store_true")
        c.set_defaults(func=cmd_cleanup)

    probe = backend.add_parser("probe", help="detect backends (starts nothing)")
    probe.add_argument("--json", action="store_true")
    probe.add_argument("--dmtcp-prefix", type=Path)
    probe.add_argument("--criu-bin", type=Path)
    probe.set_defaults(func=cmd_probe)
    for sub in (backend, top):
        add_assess(sub)
        add_selftest(sub)
        add_cleanup(sub)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.func(args))
    except BackendError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1 if exc.code not in {"BACKEND_UNAVAILABLE", "PERMISSION_DENIED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
