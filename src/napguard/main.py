"""`napguard`: the MVP daemon and its command-line client (FR-032).

    napguard daemon [--i-accept-single-user-coordinator]   # foreground, user-level
    napguard status | workloads | events [--limit N]
    napguard launch-demo [--class c-single|c-threads]
    napguard stop WORKLOAD | checkpoint WORKLOAD
    napguard checkpoints [WORKLOAD] | verify CHECKPOINT | restore CHECKPOINT --yes
    napguard schedule [--enable | --disable] [--interval SECONDS]
    napguard battery [--simulate PERCENT [--ac]] [--clear-simulation]
    napguard operations [--limit N] | run-scheduler | diagnostics
    napguard archive WORKLOAD | unarchive WORKLOAD | history | clear-history --yes
    napguard reset-demo --yes | shutdown --yes
    napguard gui [--smoke]

Operations wait for their outcome unless --no-wait is given. Exit status: 0 success,
1 the operation failed, 2 refused, invalid, or the daemon is unavailable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from napguard.checkpoint.dmtcp import DmtcpBackend
from napguard.checkpoint.policy import SINGLE_USER_FLAG, CoordinatorPolicy
from napguard.daemon.ipc import Client, IpcError, default_socket_path
from napguard.daemon.service import Service, wait_operation
from napguard.selftest.run import CLASSES


def default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "napguard"


def _socket(args: argparse.Namespace) -> Path:
    return Path(args.socket) if args.socket else default_socket_path()


def _print(data: Any, as_json: bool, text: str) -> None:
    print(json.dumps(data, indent=2, sort_keys=True, default=str) if as_json else text)


def cmd_daemon(args: argparse.Namespace) -> int:
    acknowledged = bool(args.i_accept_single_user_coordinator)
    policy = (CoordinatorPolicy.experimental_single_user(True) if acknowledged
              else CoordinatorPolicy.deny())
    if not acknowledged:
        print(f"note: started WITHOUT {SINGLE_USER_FLAG}: launch, checkpoint and restore will be "
              "refused (no DMTCP coordinator is started)", file=sys.stderr)
    service = Service(Path(args.data_dir) if args.data_dir else default_data_dir(), _socket(args),
                      DmtcpBackend(policy, prefix=args.dmtcp_prefix),
                      coordinator_acknowledged=acknowledged)
    return service.run_until_signal()


def _operation(client: Client, args: argparse.Namespace, method: str, **params: Any) -> int:
    accepted = client.call(method, **params)
    if args.no_wait:
        _print(accepted, args.json, f"accepted: operation {accepted['operation_id']}")
        return 0
    op = wait_operation(client.call, accepted["operation_id"])
    ok = op["state"] == "SUCCEEDED"
    text = (f"{op['kind']}: {op['state']} in {op.get('duration_ms')} ms"
            + ("" if ok else f" — {op.get('error_code')}: {op.get('message')}"))
    if ok and op.get("result"):
        text += "\n" + json.dumps(op["result"], indent=2, sort_keys=True, default=str)
    _print(op, args.json, text)
    return 0 if ok else 1


def cmd_client(args: argparse.Namespace) -> int:
    client = Client(_socket(args))
    cmd = args.command
    if cmd == "status":
        s = client.call("daemon.status")
        b = s["backend"]
        text = (f"daemon {s['version']} pid {s['pid']}\ncoordinators: {s['coordinator_mode']}\n"
                f"backend: {b['name']} {b['version']} available={b['available']} "
                f"verified={b['verified']}\nworkloads: {s['workloads']} "
                f"(live {s['live_instances']})\nbattery: {_battery(s['battery'])}\n"
                f"auto-checkpoint: {s['settings']['auto_checkpoint_enabled']} every "
                f"{s['settings']['auto_checkpoint_interval_s']} s\nproduction authorized: no")
        _print(s, args.json, text)
        return 0
    if cmd == "workloads":
        rows = client.call("workloads.list")
        lines = [f"{w['id']}  {w['workload_class']:<9} live={w['live']}  "
                 f"{(w['instance'] or {}).get('origin', '-')}/"
                 f"{(w['instance'] or {}).get('state', '-')}  pid "
                 f"{(w['instance'] or {}).get('pid', '-')}" for w in rows]
        _print(rows, args.json, "\n".join(lines) or "no workloads")
        return 0
    if cmd == "checkpoints":
        rows = client.call("checkpoints.list", **({"workload_id": args.workload}
                                                  if args.workload else {}))
        lines = [f"{c['id']}  {c['workload_id']}  gen {c['generation']}  {c['state']:<10} "
                 f"{c['trigger']:<16} {c['total_bytes']} B  {c['created_utc']}"
                 + ("  restore-verified" if c["restore_verified_utc"] else "") for c in rows]
        _print(rows, args.json, "\n".join(lines) or "no checkpoints")
        return 0
    if cmd == "events":
        rows = client.call("events.list", limit=args.limit)
        lines = [f"{e['ts_utc']}  {'[SIMULATED] ' if e['simulated'] else ''}{e['kind']}: "
                 f"{e['message']}" for e in reversed(rows)]
        _print(rows, args.json, "\n".join(lines) or "no events")
        return 0
    if cmd == "operations":
        rows = client.call("operations.list", limit=args.limit)
        _print(rows, args.json, "\n".join(
            f"{o['created_utc']}  {o['kind']:<17} {o['state']:<10} {o.get('trigger') or '':<24} "
            f"{o.get('workload_id') or o.get('checkpoint_id') or ''} {o.get('error_code') or ''}"
            for o in rows) or "no operations")
        return 0
    if cmd == "history":
        p = client.call("history.preview")
        lines = [f"deletable {e['workload_id']}: {e['instances']} instance(s), {e['checkpoints']} "
                 f"checkpoint(s), {e['files']} file(s), {e['bytes']} B in {e['directory']}"
                 for e in p["eligible"]]
        lines += [f"kept      {b['workload_id']}: {b['reason']}" for b in p["blocked"]]
        _print(p, args.json, "\n".join(lines) or "no history")
        return 0
    if cmd == "diagnostics":
        d = client.call("diagnostics.export")
        _print(d, args.json, f"diagnostics written to {d['path']} ({d['bytes']} B)")
        return 0
    if cmd == "run-scheduler":
        r = client.call("scheduler.run_now")
        ops = [wait_operation(client.call, o) for o in r["operation_ids"]]
        _print(ops, args.json, "\n".join(f"{o['kind']} {o['workload_id']}: {o['state']}"
                                         for o in ops))
        return 0 if all(o["state"] == "SUCCEEDED" for o in ops) else 1
    if cmd in ("archive", "unarchive"):
        return _operation(client, args, "history.archive", workload_id=args.workload,
                          archived=cmd == "archive")
    if cmd in ("clear-history", "reset-demo", "shutdown"):
        if not args.yes:
            print(f"{cmd}: add --yes to confirm (see `napguard history` for a preview)",
                  file=sys.stderr)
            return 2
        if cmd == "shutdown":
            _print(client.call("daemon.shutdown", confirm=True), args.json, "daemon stopping")
            return 0
        if cmd == "reset-demo":
            return _operation(client, args, "history.reset_demo", confirm=True)
        ids = [e["workload_id"] for e in client.call("history.preview")["eligible"]]
        if not ids:
            _print({"deleted": []}, args.json, "nothing to delete")
            return 0
        return _operation(client, args, "history.delete", workload_ids=ids, confirm=True)
    if cmd == "launch-demo":
        return _operation(client, args, "workloads.launch_demo", workload_class=args.workload_class)
    if cmd == "stop":
        return _operation(client, args, "workloads.stop", workload_id=args.workload)
    if cmd == "checkpoint":
        return _operation(client, args, "checkpoint.request", workload_id=args.workload)
    if cmd == "verify":
        return _operation(client, args, "checkpoints.verify", checkpoint_id=args.checkpoint)
    if cmd == "restore":
        if not args.yes:
            print("restore: add --yes to confirm. The workload's external files are NOT rolled "
                  "back (LIM-007)", file=sys.stderr)
            return 2
        return _operation(client, args, "recovery.restore", checkpoint_id=args.checkpoint,
                          confirm=True)
    if cmd == "schedule":
        changes: dict[str, Any] = {}
        if args.enable or args.disable:
            changes["auto_checkpoint_enabled"] = bool(args.enable)
        if args.interval is not None:
            changes["auto_checkpoint_interval_s"] = args.interval
        s = client.call("settings.set", **changes) if changes else client.call("settings.get")
        _print(s, args.json, f"auto-checkpoint: {s['auto_checkpoint_enabled']} every "
               f"{s['auto_checkpoint_interval_s']} s")
        return 0
    # battery
    if args.clear_simulation:
        b = client.call("battery.simulate", clear=True)
    elif args.simulate is not None:
        b = client.call("battery.simulate", percent=args.simulate, ac_online=args.ac)
    else:
        b = client.call("battery.status")
    _print(b, args.json, _battery(b))
    return 0


def _battery(b: dict[str, Any]) -> str:
    if not b.get("available"):
        return f"unavailable ({b.get('detail') or b.get('source')})"
    label = "SIMULATED " if b.get("simulated") else ""
    return (f"{label}{b['percent']}% charging={b['charging']} AC={b['ac_online']} "
            f"(source {b['source']})")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="napguard", description="NapGuard MVP (developer mode)")
    p.add_argument("--socket", help="daemon socket (default $XDG_RUNTIME_DIR/napguard/daemon.sock)")
    p.add_argument("--json", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("daemon", help="run the user-level daemon in the foreground")
    d.add_argument(SINGLE_USER_FLAG, dest="i_accept_single_user_coordinator", action="store_true")
    d.add_argument("--data-dir")
    d.add_argument("--dmtcp-prefix", type=Path)
    for name in ("status", "workloads"):
        sub.add_parser(name)
    e = sub.add_parser("events")
    e.add_argument("--limit", type=int, default=50)
    ld = sub.add_parser("launch-demo", help="launch a verified reference workload")
    ld.add_argument("--class", dest="workload_class", choices=sorted(CLASSES), default="c-single")
    for name, arg in (("stop", "workload"), ("checkpoint", "workload"),
                      ("verify", "checkpoint"), ("restore", "checkpoint")):
        s = sub.add_parser(name)
        s.add_argument(arg)
        if name == "restore":
            s.add_argument("--yes", action="store_true", help="confirm the restore (FR-023)")
    for s in (ld, *(sub.choices[n] for n in ("stop", "checkpoint", "verify", "restore"))):
        s.add_argument("--no-wait", action="store_true")
    c = sub.add_parser("checkpoints")
    c.add_argument("workload", nargs="?")
    sc = sub.add_parser("schedule")
    group = sc.add_mutually_exclusive_group()
    group.add_argument("--enable", action="store_true")
    group.add_argument("--disable", action="store_true")
    sc.add_argument("--interval", type=int)
    ops = sub.add_parser("operations")
    ops.add_argument("--limit", type=int, default=30)
    for name in ("history", "diagnostics", "run-scheduler"):
        sub.add_parser(name)
    for name in ("archive", "unarchive"):
        a = sub.add_parser(name)
        a.add_argument("workload")
        a.add_argument("--no-wait", action="store_true")
    for name in ("clear-history", "reset-demo", "shutdown"):
        y = sub.add_parser(name)
        y.add_argument("--yes", action="store_true")
        y.add_argument("--no-wait", action="store_true")
    b = sub.add_parser("battery")
    b.add_argument("--simulate", type=int, metavar="PERCENT",
                   help="SIMULATED reading for demonstration only")
    b.add_argument("--ac", action="store_true")
    b.add_argument("--clear-simulation", action="store_true")
    g = sub.add_parser("gui", help="open the desktop GUI")
    g.add_argument("--smoke", action="store_true",
                   help="render every screen for a few seconds, then exit (needs a display)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "daemon":
            return cmd_daemon(args)
        if args.command == "gui":
            from napguard.ui.gui import main as gui_main

            return gui_main(str(_socket(args)), smoke=bool(args.smoke))
        return cmd_client(args)
    except IpcError as exc:
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
