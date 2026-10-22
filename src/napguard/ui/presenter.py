"""GUI view-model: every decision the screens make, as pure functions (no tkinter).

The daemon's records are the only source of truth. Friendly labels are DERIVED from
real backend states and pending operations; nothing here invents a state, a value, or
an outcome. Missing information is shown as "Not available", never as zero.
"""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass, field
from typing import Any

NA = "Not available"

# --------------------------------------------------------------- process states

STATE_INFO: dict[str, tuple[str, str]] = {  # label -> (colour role, explanation)
    "STARTING": ("info", "NapGuard is launching the workload under DMTCP."),
    "RUNNING": ("ok", "The workload runs under NapGuard's control and can be checkpointed."),
    "CHECKPOINTING": ("busy", "A DMTCP checkpoint of this workload is in progress."),
    "STOPPING": ("busy", "NapGuard is terminating this workload."),
    "STOPPED": ("muted", "Stopped through NapGuard. Its newest checkpoint can be restored."),
    "RESTORING": ("busy", "DMTCP is restoring this workload from a checkpoint."),
    "RESTORED": ("ok", "Running again from a checkpoint; continuity was verified. Restored "
                       "instances are not checkpointed again in this version."),
    "EXITED": ("warn", "The process ended on its own (it was not stopped by NapGuard)."),
    "FAILED": ("error", "The last launch or restore did not complete or could not be verified."),
    "LOST": ("warn", "The daemon restarted while this was running; it is no longer tracked."),
    "NEVER STARTED": ("muted", "No instance has been recorded."),
}


def display_state(w: dict[str, Any]) -> str:
    """Friendly state from the latest instance and any pending operation of a workload."""
    pending = w.get("pending_operation") or {}
    if pending.get("state") == "RUNNING":
        return {"checkpoint": "CHECKPOINTING", "restore": "RESTORING",
                "stop": "STOPPING", "launch": "STARTING"}.get(str(pending.get("kind")),
                                                             _instance_state(w))
    return _instance_state(w)


def _instance_state(w: dict[str, Any]) -> str:
    inst = w.get("instance") or {}
    state = str(inst.get("state") or "NEVER STARTED")
    if state == "RUNNING" and inst.get("origin") == "RESTORE":
        return "RESTORED"
    return state


@dataclass(frozen=True)
class Action:
    enabled: bool
    reason: str


def workload_actions(w: dict[str, Any] | None) -> dict[str, Action]:
    if w is None:
        none = Action(False, "Select a workload first.")
        return {k: none for k in ("checkpoint", "stop", "archive", "unarchive", "delete")}
    inst = w.get("instance") or {}
    live = bool(w.get("live"))
    busy = w.get("pending_operation") is not None
    restored = inst.get("origin") == "RESTORE"
    checkpoint = (Action(False, "An operation for this workload is in progress.") if busy else
                  Action(False, "Only a running workload can be checkpointed.") if not live else
                  Action(False, "Restored instances are not checkpointed in this version "
                                "(ADR 0008).") if restored else
                  Action(True, "Save the process's execution state with DMTCP now."))
    stop = (Action(False, "An operation for this workload is in progress.") if busy else
            Action(True, "Terminate this workload's process group and coordinator.") if live else
            Action(False, "Nothing is running for this workload."))
    finished = not live and not busy
    archived = bool(w.get("archived"))
    return {
        "checkpoint": checkpoint,
        "stop": stop,
        "archive": Action(finished and not archived,
                          "Hide this finished record (nothing is deleted)." if finished else
                          "Stop the workload before archiving it."),
        "unarchive": Action(archived, "Show this record in the default list again."),
        "delete": Action(finished, "Permanently delete this finished workload's history."
                         if finished else "Running or busy workloads cannot be deleted."),
    }


# ------------------------------------------------------------------ checkpoints

CHECKPOINT_INFO: dict[str, tuple[str, str, str]] = {  # state -> (label, colour, meaning)
    "PUBLISHED": ("PUBLISHED", "ok", "Complete, recorded in the catalog, and the newest "
                  "generation of its instance."),
    "SUPERSEDED": ("SUPERSEDED", "muted", "A newer checkpoint of the same instance overwrote "
                   "this image. Kept as history; not restorable."),
    "CORRUPT": ("INVALID", "error", "Integrity verification failed (manifest or image changed). "
                "Never restored."),
    "MISSING": ("MISSING", "warn", "Its files are gone, so it cannot be restored."),
}


def checkpoint_label(c: dict[str, Any]) -> str:
    return CHECKPOINT_INFO.get(str(c.get("state")), (str(c.get("state")), "", ""))[0]


def integrity_text(c: dict[str, Any]) -> str:
    if c.get("state") == "CORRUPT":
        return "FAILED"
    return "verified " + short_time(c["verified_utc"]) if c.get("verified_utc") else "not verified"


def restore_history_text(c: dict[str, Any]) -> str:
    restores = c.get("restores") or []
    if not restores:
        return "never restored"
    ok = sum(1 for r in restores if r.get("state") in ("RUNNING", "STOPPED"))
    return f"{len(restores)} restore(s), {ok} verified"


# --------------------------------------------------------------------- activity

ERROR_KINDS = ("failed", "corrupt", "rejected", "delete_failed")
WARNING_PREFIXES = ("battery.", "daemon.shutdown", "workload.exited", "history.delete_failed")


def severity(e: dict[str, Any]) -> str:
    kind = str(e.get("kind", ""))
    if any(kind.endswith(k) or f".{k}" in kind for k in ERROR_KINDS):
        return "Error"
    if e.get("simulated") or kind.startswith(WARNING_PREFIXES):
        return "Warning"
    return "Success"


FRIENDLY: dict[str, str] = {
    "checkpoint.published": "Checkpoint created successfully.",
    "restore.verified": "Restore verified: original nonce and a rolled-back, advancing counter.",
    "restore.failed": "Restoration could not be verified.",
    "workload.launched": "Demo workload launched.",
    "workload.stopped": "Workload stopped.",
    "checkpoint.verified": "Checkpoint integrity verified.",
    "checkpoint.corrupt": "Checkpoint integrity verification FAILED.",
    "demo.reset": "Demo history reset.",
}


def event_workload(e: dict[str, Any]) -> str:
    data = e.get("data") or {}
    return str(data.get("workload_id") or data.get("checkpoint_id") or "")


def event_text(e: dict[str, Any]) -> str:
    prefix = "[SIMULATED] " if e.get("simulated") else ""
    friendly = FRIENDLY.get(str(e.get("kind")))
    message = str(e.get("message", ""))
    return prefix + (f"{friendly} {message}" if friendly else message)


def matches(e: dict[str, Any], level: str, search: str) -> bool:
    if level != "All" and severity(e) != level:
        return False
    if search:
        hay = " ".join([str(e.get("kind")), str(e.get("message")), event_workload(e)]).lower()
        return search.lower() in hay
    return True


ACTIONS: dict[str, str] = {  # UX-006: every error code with a suggested action
    "DAEMON_UNAVAILABLE": "Start the daemon (Settings & Help, or the banner button).",
    "PERMISSION_DENIED": "Start the daemon in developer mode (acknowledgement required), on a "
                         "single-user VM. If it mentions exposure, check the Activity log.",
    "CONFLICT": "Stop the workload, or wait for the running operation, then try again.",
    "INVALID_STATE": "Only the newest PUBLISHED checkpoint of a stopped workload can be restored.",
    "VERIFY_FAILED": "The restore was not accepted. View technical details; stop any stray "
                     "process and try again. NapGuard never marks an unverified restore successful.",
    "RESTORE_FAILED": "DMTCP could not restore. View technical details (restarter exit status, "
                      "logs).",
    "CHECKPOINT_FAILED": "The checkpoint was not verified complete. The workload kept running; "
                         "try again.",
    "CORRUPT": "The checkpoint changed after capture and will never be restored. Delete it.",
    "INVALID_MANIFEST": "The manifest is damaged; this checkpoint cannot be restored.",
    "UNSUPPORTED_WORKLOAD": "Only the verified reference workloads c-single and c-threads are "
                            "supported.",
    "BACKEND_UNAVAILABLE": "Build DMTCP 4.2.0 with experiments/stage02/build-dmtcp.sh.",
    "NOT_RUNNING": "Launch a demo workload first.",
    "TIMED_OUT": "The operation took too long; check the Activity log, then retry.",
    "INVALID_ARGUMENT": "Check the values you entered.",
}


def suggested_action(code: str | None) -> str:
    return ACTIONS.get(str(code), "See the Activity log and View Technical Details.")


# -------------------------------------------------------------------- dashboard

def card_values(status: dict[str, Any] | None, connected: bool) -> list[tuple[str, str, str]]:
    """(title, value, colour) for the dashboard cards; unavailable is never shown as 0."""
    if not connected or status is None:
        return [("Daemon", "Not running", "error")] + [
            (t, NA, "muted") for t in ("DMTCP backend", "Active processes", "Saved checkpoints",
                                       "Automatic checkpoints", "Battery")]
    b = status.get("backend") or {}
    battery = status.get("battery") or {}
    developer = "developer" in str(status.get("coordinator_mode", ""))
    s = status.get("settings") or {}
    return [
        ("Daemon", "Running (developer mode)" if developer else "Running (refused mode)",
         "ok" if developer else "warn"),
        ("DMTCP backend", f"{b.get('version') or NA} {'verified' if b.get('verified') else ''}"
         .strip() if b.get("available") else "Unavailable", "ok" if b.get("verified") else "warn"),
        ("Active processes", str(status.get("live_instances")), "info"),
        ("Saved checkpoints", str(status.get("checkpoints", NA)), "info"),
        ("Automatic checkpoints", f"On, every {s.get('auto_checkpoint_interval_s')} s"
         if s.get("auto_checkpoint_enabled") else "Off", "ok" if s.get("auto_checkpoint_enabled")
         else "muted"),
        ("Battery", battery_short(battery), "warn" if battery.get("simulated") else
         "info" if battery.get("available") else "muted"),
    ]


def battery_short(b: dict[str, Any]) -> str:
    if not b.get("available"):
        return "Unavailable"
    return f"{'SIMULATED ' if b.get('simulated') else ''}{b.get('percent')}%"


def battery_long(b: dict[str, Any]) -> str:
    if not b.get("available"):
        return ("Battery unavailable: this virtual machine does not expose battery hardware."
                if b.get("source") == "NONE" else f"Battery unavailable ({b.get('detail')}).")
    label = "SIMULATED reading (demonstration only, not real hardware)" if b.get("simulated") \
        else "Real battery (sysfs)"
    state = "charging" if b.get("charging") or b.get("ac_online") else "discharging"
    return f"{label}: {b.get('percent')}%, {state}."


# ------------------------------------------------------------------ guided demo

STEPS = ("Launch process", "Observe process", "Create checkpoint", "Stop original",
         "Verify checkpoint", "Restore", "Verify recovery")
STATUSES = ("Pending", "Running", "Passed", "Failed", "Blocked")


@dataclass
class Step:
    title: str
    status: str = "Pending"
    detail: str = ""


@dataclass
class GuidedDemo:
    """Strictly sequential: a step can run only after every earlier step PASSED."""

    steps: list[Step] = field(default_factory=lambda: [Step(t) for t in STEPS])
    values: dict[str, Any] = field(default_factory=dict)

    def next_index(self) -> int | None:
        for i, step in enumerate(self.steps):
            if step.status != "Passed":
                return i if step.status in ("Pending",) else None
        return None

    def can_run(self, i: int) -> bool:
        return (self.steps[i].status == "Pending"
                and all(s.status == "Passed" for s in self.steps[:i]))

    def start(self, i: int) -> None:
        if not self.can_run(i):
            raise ValueError(f"step {i + 1} cannot run now")
        self.steps[i].status = "Running"

    def finish(self, i: int, ok: bool, detail: str) -> None:
        if self.steps[i].status != "Running":
            raise ValueError(f"step {i + 1} is not running")
        self.steps[i].status = "Passed" if ok else "Failed"
        self.steps[i].detail = detail
        if not ok:
            for later in self.steps[i + 1:]:
                later.status = "Blocked"
                later.detail = f"Blocked: step {i + 1} did not pass."

    def block(self, i: int, reason: str) -> None:
        self.steps[i].status = "Blocked"
        self.steps[i].detail = reason

    def done(self) -> bool:
        return all(s.status == "Passed" for s in self.steps)


def recovery_rows(result: dict[str, Any] | None) -> list[tuple[str, str]]:
    """The recovery evidence, exactly as the daemon reported it."""
    r = result or {}

    def v(key: str) -> str:
        value = r.get(key)
        return NA if value is None or value == "" else str(value)

    verified = r.get("continuity_verified")
    return [("Original PID", v("original_pid")), ("Restored PID", v("restored_pid")),
            ("Original nonce", v("original_nonce")), ("Restored nonce", v("restored_nonce")),
            ("Counter at checkpoint", v("counter_at_checkpoint")),
            ("Counter before termination", v("counter_before_termination")),
            ("First restored counter", v("first_restored_counter")),
            ("Counter after restore", v("last_restored_counter")),
            ("Continuity verification", NA if verified is None else
             ("PASSED" if verified else "FAILED"))]


# ---------------------------------------------------------------------- format

def human_bytes(n: int | None) -> str:
    if n is None:
        return NA
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def short_time(utc: str | None) -> str:
    if not utc:
        return NA
    return utc.replace("T", " ").replace("Z", " UTC")


def age(utc: str | None, now: float | None = None) -> str:
    if not utc:
        return NA
    seconds = int((now if now is not None else time.time())
                  - calendar.timegm(time.strptime(utc, "%Y-%m-%dT%H:%M:%SZ")))
    if seconds < 60:
        return f"{seconds} s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min ago"


LIMITATIONS = (
    "Developer mode only, on a single-user Ubuntu VM: the DMTCP coordinator is unauthenticated "
    "(ADR 0006/0008). Not production-authorized.",
    "Only the verified reference workloads c-single and c-threads. No Python, network, GUI, or "
    "multi-process programs.",
    "Files and other external state are NOT rolled back by a restore (LIM-007).",
    "Only the newest checkpoint of a stopped instance is restorable; restored instances are not "
    "checkpointed again.",
    "A VM usually has no battery: battery events here are SIMULATED and labelled as such.",
    "No suspend/resume integration; stopping the daemon stops its workloads.",
)
