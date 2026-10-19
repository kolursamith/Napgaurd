"""Command line for the Stage 02 experiments. Linux only."""

from __future__ import annotations

import argparse
import json
import os
import platform
from pathlib import Path

from . import trial
from .backends import Criu, Dmtcp, tool_version
from .results import EXIT_CODES, Status
from .runner import Recorder, environment, git_revision


def default_run_root() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "napguard-stage02" / "runs"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run_stage02.py", description=__doc__)
    p.add_argument("--run-root", type=Path, default=default_run_root(),
                   help="where run directories are created (default: %(default)s)")
    p.add_argument("--dmtcp-prefix", type=Path, help="DMTCP install prefix (contains bin/)")
    p.add_argument("--criu-bin", type=Path, help="path to the criu executable")
    p.add_argument("--criu-mode", choices=["unprivileged", "sudo"], default="unprivileged",
                   help="'sudo' runs criu through `sudo -n` (diagnostic only; requires approval)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("env", help="record environment and backend availability (no experiments)")

    t = sub.add_parser("trial", help="checkpoint/kill/restore one workload class")
    t.add_argument("--backend", choices=["dmtcp", "criu"], required=True)
    t.add_argument("--class", dest="workload_class", choices=sorted(trial.CLASSES),
                   default="c-single")

    m = sub.add_parser("matrix", help="run trials for several workload classes")
    m.add_argument("--backend", choices=["dmtcp", "criu"], required=True)
    m.add_argument("--classes", nargs="+", choices=sorted(trial.CLASSES),
                   default=list(trial.CLASSES))

    r = sub.add_parser("reboot-prepare", help="launch, checkpoint, terminate; then reboot manually")
    r.add_argument("--backend", choices=["dmtcp", "criu"], required=True)
    r.add_argument("--class", dest="workload_class", choices=sorted(trial.CLASSES),
                   default="c-single")

    s = sub.add_parser("reboot-resume", help="after a reboot: verify artifacts, restore, verify")
    s.add_argument("--run-dir", type=Path, required=True)
    return p


def cmd_env(args: argparse.Namespace) -> Status:
    args.run_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    rec = Recorder(args.run_root.parent / "env-latest")
    report: dict[str, object] = {"environment": environment(args.run_root),
                                 "git": git_revision(trial.REPO)}
    probes = {
        "dmtcp": Dmtcp(rec, args.dmtcp_prefix).probe(),
        "criu": Criu(rec, args.criu_bin, args.criu_mode).probe(),
    }
    for name, check in probes.items():
        report[name] = check.to_json()
    report["cc"] = tool_version(["cc", "--version"])
    (rec.run_dir / "env.json").write_text(json.dumps(report, indent=2, sort_keys=True),
                                          encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    print(f"\nwritten: {rec.run_dir / 'env.json'}")
    # PASS here only means at least one backend is available; it is not a checkpoint result.
    return Status.PASS if any(c.status is Status.PASS for c in probes.values()) else Status.BLOCKED


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if platform.system() != "Linux":
        print(f"BLOCKED: Stage 02 experiments run only on Linux (this is {platform.system()}).")
        return EXIT_CODES[Status.BLOCKED]
    args.run_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def opts(workload_class: str = "c-single") -> trial.Options:
        return trial.Options(backend=args.backend, workload_class=workload_class,
                             run_root=args.run_root, dmtcp_prefix=args.dmtcp_prefix,
                             criu_bin=args.criu_bin, criu_mode=args.criu_mode)

    if args.command == "env":
        status = cmd_env(args)
    elif args.command == "trial":
        status = trial.run_trial(opts(args.workload_class))
    elif args.command == "matrix":
        status = trial.run_matrix(opts(), args.classes)
    elif args.command == "reboot-prepare":
        status = trial.reboot_prepare(opts(args.workload_class))
    else:
        status = trial.reboot_resume(args.run_dir)
    return EXIT_CODES[status]
