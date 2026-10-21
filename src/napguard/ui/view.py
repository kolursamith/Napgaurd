"""Display formatting for the GUI (no tkinter import, so it is testable on any host)."""

from __future__ import annotations

from typing import Any

WORKLOAD_COLUMNS = ("id", "class", "live", "instance", "state", "pid")
CHECKPOINT_COLUMNS = ("id", "workload", "gen", "state", "trigger", "size", "created", "restored")
EVENT_COLUMNS = ("time", "kind", "message")


def workload_row(w: dict[str, Any]) -> tuple[str, ...]:
    inst = w.get("instance") or {}
    return (str(w["id"]), str(w["workload_class"]), "yes" if w.get("live") else "no",
            str(inst.get("origin", "-")), str(inst.get("state", "-")), str(inst.get("pid") or "-"))


def checkpoint_row(c: dict[str, Any]) -> tuple[str, ...]:
    return (str(c["id"]), str(c["workload_id"]), str(c["generation"]), str(c["state"]),
            str(c["trigger"]), human_bytes(int(c["total_bytes"])), str(c["created_utc"]),
            "verified" if c.get("restore_verified_utc") else "-")


def event_row(e: dict[str, Any]) -> tuple[str, ...]:
    prefix = "[SIMULATED] " if e.get("simulated") else ""
    return (str(e["ts_utc"]), str(e["kind"]), prefix + str(e["message"]))


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def battery_text(b: dict[str, Any]) -> str:
    if not b.get("available"):
        return f"Battery: unavailable — {b.get('detail') or 'no battery exposed'}"
    label = "SIMULATED (demonstration only) — " if b.get("simulated") else "Real (sysfs) — "
    charging = "charging" if b.get("charging") else "discharging"
    ac = "AC online" if b.get("ac_online") else "on battery"
    return f"{label}{b.get('percent')}%, {charging}, {ac}"


def dashboard_lines(status: dict[str, Any]) -> list[str]:
    b = status.get("backend", {})
    s = status.get("settings", {})
    return [
        f"Daemon: running (pid {status.get('pid')}, version {status.get('version')})",
        f"Coordinators: {status.get('coordinator_mode')}",
        f"Backend: {b.get('name')} {b.get('version')} — available={b.get('available')}, "
        f"verified={b.get('verified')}",
        f"Workloads: {status.get('workloads')} (live instances {status.get('live_instances')})",
        f"Automatic checkpoints: {'ON' if s.get('auto_checkpoint_enabled') else 'off'}, every "
        f"{s.get('auto_checkpoint_interval_s')} s",
        battery_text(status.get("battery", {})),
        "Developer mode only. NOT production-authorized. External files are not rolled back "
        "by a restore (LIM-007).",
    ]


def operation_text(op: dict[str, Any]) -> str:
    if op.get("state") == "SUCCEEDED":
        return f"{op.get('kind')} succeeded in {op.get('duration_ms')} ms"
    return f"{op.get('kind')} {op.get('state')}: {op.get('error_code')}: {op.get('message')}"
