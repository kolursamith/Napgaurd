"""Command line for the Stage 02 experiments. Linux only."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
from pathlib import Path

from . import trial
from .backends import Criu, Dmtcp, tool_version
from .results import EXIT_CODES, Status
from .runner import Recorder, environment, git_revision, read_text


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
    t.add_argument("--adr-0004-controlled-trial", dest="dmtcp_controlled_trial",
                   action="store_true",
                   help="run ONE DMTCP c-single trial under the proposed ADR 0004 exception "
                        "(requires explicit approval; loopback hand-off and listener audits)")

    m = sub.add_parser("matrix", help="run trials for several workload classes")
    m.add_argument("--backend", choices=["dmtcp", "criu"], required=True)
    m.add_argument("--classes", nargs="+", choices=sorted(trial.CLASSES),
                   default=list(trial.CLASSES))

    adr_help = ("ADR 0004 controlled exception: DMTCP c-single only, loopback hand-off, "
                "listener audits (requires explicit approval)")
    r = sub.add_parser("reboot-prepare", help="launch, checkpoint, terminate; then reboot manually")
    r.add_argument("--backend", choices=["dmtcp", "criu"], required=True)
    r.add_argument("--class", dest="workload_class", choices=sorted(trial.CLASSES),
                   default="c-single")
    r.add_argument("--adr-0004-controlled-trial", dest="dmtcp_controlled_trial",
                   action="store_true", help=adr_help)

    s = sub.add_parser("reboot-resume", help="after a reboot: verify artifacts, restore, verify")
    s.add_argument("--run-dir", type=Path,
                   help="prepared run (default: newest run with a manifest not yet resumed)")
    s.add_argument("--backend", choices=["dmtcp", "criu"],
                   help="must match the prepared run (default: from its manifest)")
    s.add_argument("--class", dest="workload_class", choices=sorted(trial.CLASSES),
                   help="must match the prepared run (default: from its manifest)")
    s.add_argument("--adr-0004-controlled-trial", dest="dmtcp_controlled_trial",
                   action="store_true", help=adr_help)

    z = sub.add_parser("summarize", help="read-only: tabulate recorded evidence from result.json")
    z.add_argument("--json", action="store_true", help="print JSON instead of a table")

    sub.add_parser("dmtcp-isolation-check",
                   help="start ONE DMTCP coordinator on a pre-bound 127.0.0.1 listener, prove it "
                        "is unreachable from other addresses, stop it (no workload, no checkpoint)")
    return p


def netns_probe(rec: Recorder) -> dict[str, object]:
    """Informational OD-13 input: can this user create a network namespace with only `lo`?

    Creates and immediately discards a namespace; changes nothing persistent.
    """
    result: dict[str, object] = {
        "apparmor_restrict_unprivileged_userns": read_text(
            "/proc/sys/kernel/apparmor_restrict_unprivileged_userns"),
        "unprivileged_userns_clone": read_text("/proc/sys/kernel/unprivileged_userns_clone"),
    }
    for name, argv in (
        ("unshare", ["unshare", "--user", "--map-root-user", "--net", "--fork", "--",
                     "sh", "-c", "ip link set lo up && ip -br addr show"]),
        ("bwrap", ["bwrap", "--unshare-net", "--ro-bind", "/", "/", "--dev", "/dev",
                   "--proc", "/proc", "--", "sh", "-c", "ip -br addr show"]),
    ):
        if shutil.which(argv[0]) is None:
            result[name] = "not installed"
            continue
        res = rec.run(f"netns-{name}", argv, timeout=15)
        result[name] = {"exit": res.exit_code, "output": (res.stdout() + res.stderr()).strip()}
    return result


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
    report["network_namespace_probe"] = netns_probe(rec)
    (rec.run_dir / "env.json").write_text(json.dumps(report, indent=2, sort_keys=True),
                                          encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    print(f"\nwritten: {rec.run_dir / 'env.json'}")
    # PASS here only means at least one backend is available; it is not a checkpoint result.
    return Status.PASS if any(c.status is Status.PASS for c in probes.values()) else Status.BLOCKED


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "summarize":  # read-only; safe on any host
        rows = trial.summarize_runs(args.run_root)
        if args.json:
            print(json.dumps(rows, indent=2, sort_keys=True))
        else:
            keys = ("run", "kind", "status", "class", "checkpoint_s", "image_bytes",
                    "restore_to_first_report_s", "kill_counter", "restored_first_counter",
                    "restored_last_counter")
            print("\t".join(keys))
            for row in rows:
                print("\t".join("-" if row.get(k) is None else str(row.get(k)) for k in keys))
        return EXIT_CODES[Status.PASS if rows else Status.SKIP]
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
        options = opts(args.workload_class)
        options.dmtcp_controlled_trial = bool(args.dmtcp_controlled_trial)
        status = trial.run_trial(options)
    elif args.command == "matrix":
        status = trial.run_matrix(opts(), args.classes)
    elif args.command == "reboot-prepare":
        options = opts(args.workload_class)
        options.dmtcp_controlled_trial = bool(args.dmtcp_controlled_trial)
        status = trial.reboot_prepare(options)
    elif args.command == "dmtcp-isolation-check":
        status = trial.dmtcp_isolation_check(args.run_root, args.dmtcp_prefix)
    else:
        status = trial.reboot_resume(args.run_dir, args.run_root, backend_name=args.backend,
                                     workload_class=args.workload_class,
                                     controlled=bool(args.dmtcp_controlled_trial))
    return EXIT_CODES[status]
