"""NapGuard desktop application (Tkinter + ttk): a client of the user-level daemon.

* Every daemon call runs on a background worker; the window never blocks.
* What is shown comes from the daemon. Success is shown only after the daemon reports it.
* Simulated battery readings and events are labelled SIMULATED everywhere.
* The GUI can start the daemon in developer mode only after an explicit acknowledgement
  in a dialog, for that run only (ADR 0006/0008); it is never stored.
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
import time
import tkinter as tk
import traceback
from collections.abc import Callable, Sequence
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from napguard import __version__
from napguard.daemon.ipc import Client, IpcError
from napguard.daemon.service import wait_operation
from napguard.selftest.run import CLASSES

from . import presenter as pr
from . import theme
from .worker import Worker

REFRESH_MS = 2000
PAGES = ("Dashboard", "Processes", "Checkpoints", "Recovery", "Automation", "Simulation Lab",
         "Activity", "Settings & Help")
ICONS = {"Dashboard": "◈", "Processes": "▶", "Checkpoints": "◉", "Recovery": "⟲",
         "Automation": "⏱", "Simulation Lab": "⚗", "Activity": "☰", "Settings & Help": "⚙",
         "Guided Demo": "★"}


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "napguard" / "gui.json"


def load_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


class Tooltip:
    def __init__(self, widget: tk.Widget, text: Callable[[], str] | str) -> None:
        self.widget, self.text = widget, text
        self.tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")

    def show(self, _event: object = None) -> None:
        text = self.text() if callable(self.text) else self.text
        if not text or self.tip is not None:
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        tip.wm_geometry(f"+{x}+{y}")
        self.tip = tip
        tk.Label(tip, text=text, justify="left", wraplength=360, bg="#1e293b", fg="#e2e8f0",
                 padx=8, pady=6).pack()

    def hide(self, _event: object = None) -> None:
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


class Store:
    def __init__(self) -> None:
        self.connected = False
        self.error: IpcError | None = None
        self.status: dict[str, Any] | None = None
        self.workloads: list[dict[str, Any]] = []
        self.checkpoints: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.scheduler: dict[str, Any] = {}
        self.operations: list[dict[str, Any]] = []


def make_table(parent: tk.Misc, columns: tuple[str, ...], widths: tuple[int, ...],
               height: int = 10) -> tuple[ttk.Frame, ttk.Treeview]:
    frame = ttk.Frame(parent, style="Panel.TFrame")
    tree = ttk.Treeview(frame, columns=columns, show="headings", height=height,
                        selectmode="browse")
    for col, width in zip(columns, widths, strict=True):
        tree.heading(col, text=col)
        tree.column(col, width=width, anchor="w", stretch=True)
    bar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=bar.set)
    tree.pack(side="left", fill="both", expand=True)
    bar.pack(side="right", fill="y")
    return frame, tree


def fill(tree: ttk.Treeview, rows: Sequence[tuple[str, Sequence[str]]]) -> None:
    """Replace rows (iid, values) and keep the selection when the row still exists."""
    selected = tree.selection()
    tree.delete(*tree.get_children())
    for iid, values in rows:
        tree.insert("", "end", iid=iid, values=tuple(values))
    keep = [s for s in selected if tree.exists(s)]
    if keep:
        tree.selection_set(keep)


class Page(ttk.Frame):
    title = ""
    subtitle = ""

    def __init__(self, parent: tk.Misc, app: App) -> None:
        super().__init__(parent, padding=(24, 18))
        self.app = app
        ttk.Label(self, text=self.title, style="Title.TLabel").pack(anchor="w")
        ttk.Label(self, text=self.subtitle, style="Subtitle.TLabel", wraplength=900,
                  justify="left").pack(anchor="w", pady=(0, 12))

    def update_view(self, store: Store) -> None:
        return None


def card(parent: tk.Misc, title: str) -> tuple[ttk.Frame, ttk.Label]:
    frame = ttk.Frame(parent, style="Card.TFrame", padding=14)
    ttk.Label(frame, text=title.upper(), style="CardMuted.TLabel").pack(anchor="w")
    value = ttk.Label(frame, text=pr.NA, style="CardValue.TLabel", wraplength=230)
    value.pack(anchor="w", pady=(4, 0))
    return frame, value


def text_box(parent: tk.Misc, height: int, mode: str) -> tk.Text:
    p = theme.PALETTES[mode]
    box = tk.Text(parent, height=height, wrap="word", bg=p["panel"], fg=p["text"],
                  insertbackground=p["text"], relief="flat", padx=10, pady=8,
                  highlightthickness=1, highlightbackground=p["line"])
    return box


def set_text(box: tk.Text, text: str) -> None:
    box.configure(state="normal")
    box.delete("1.0", "end")
    box.insert("end", text)
    box.configure(state="disabled")


# ------------------------------------------------------------------ dashboard


class DashboardPage(Page):
    title = "Dashboard"
    subtitle = ("Live status of the NapGuard daemon, the DMTCP backend, and your managed demo "
                "processes. Values come from the daemon; unavailable values say so.")

    def __init__(self, parent: tk.Misc, app: App) -> None:
        super().__init__(parent, app)
        grid = ttk.Frame(self)
        grid.pack(fill="x")
        self.cards: list[ttk.Label] = []
        for i, name in enumerate(("Daemon", "DMTCP backend", "Active processes",
                                  "Saved checkpoints", "Automatic checkpoints", "Battery")):
            frame, value = card(grid, name)
            frame.grid(row=i // 3, column=i % 3, sticky="nsew", padx=6, pady=6)
            grid.columnconfigure(i % 3, weight=1)
            self.cards.append(value)
        quick = ttk.Frame(self)
        quick.pack(fill="x", pady=(10, 6))
        ttk.Label(quick, text="Quick actions", style="Section.TLabel").pack(side="left",
                                                                           padx=(0, 12))
        ttk.Button(quick, text="▶ Launch Demo Process", style="Accent.TButton",
                   command=lambda: app.launch("c-single")).pack(side="left", padx=4)
        ttk.Button(quick, text="◉ Create Checkpoint",
                   command=app.checkpoint_first_running).pack(side="left", padx=4)
        ttk.Button(quick, text="⟲ Open Recovery",
                   command=lambda: app.show("Recovery")).pack(side="left", padx=4)
        ttk.Button(quick, text="★ Start Guided Demo",
                   command=lambda: app.show("Guided Demo")).pack(side="left", padx=4)
        lower = ttk.Frame(self)
        lower.pack(fill="both", expand=True, pady=(8, 0))
        left = ttk.Frame(lower)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        right = ttk.Frame(lower)
        right.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="Recent activity", style="Section.TLabel").pack(anchor="w")
        frame, self.recent = make_table(left, ("Time", "Event"), (150, 420), height=8)
        frame.pack(fill="both", expand=True, pady=(4, 0))
        ttk.Label(right, text="System status", style="Section.TLabel").pack(anchor="w")
        self.system = text_box(right, 12, app.mode)
        self.system.pack(fill="both", expand=True, pady=(4, 0))

    def update_view(self, store: Store) -> None:
        for label, (_title, value, role) in zip(self.cards, pr.card_values(store.status,
                                                                            store.connected),
                                                strict=True):
            label.configure(text=value, foreground=theme.colour(self.app.mode, role))
        fill(self.recent, [(str(e["seq"]), (pr.short_time(e["ts_utc"]), pr.event_text(e)))
                           for e in store.events[:12]])
        s = store.status or {}
        latest = s.get("latest_checkpoint") or {}
        restore = (s.get("latest_restore") or [None])[0] or {}
        busy = s.get("busy") or {}
        lines = [
            f"Latest successful checkpoint: {latest.get('id', pr.NA)} "
            f"({pr.age(latest.get('created_utc'))})" if latest else
            "Latest successful checkpoint: none yet",
            "Latest restore outcome: " + (f"{restore.get('state')} "
                                          f"({pr.short_time(restore.get('ended_utc'))})"
                                          if restore else "no restore yet"),
            f"Operation in progress: {busy.get('kind')} ({busy.get('state')})" if busy else
            "Operation in progress: none",
            f"Coordinator mode: {s.get('coordinator_mode', pr.NA)}",
            "", "Current limitations:", *[f" • {item}" for item in pr.LIMITATIONS]]
        set_text(self.system, "\n".join(lines) if store.connected else
                 "The daemon is not reachable. Use the banner above to start it.")


# ------------------------------------------------------------------ processes


class ProcessesPage(Page):
    title = "Processes"
    subtitle = ("Managed demo workloads. NapGuard launches only its verified reference "
                "workloads and never attaches to other processes. Hover a button to see why it "
                "is enabled or not.")

    def __init__(self, parent: tk.Misc, app: App) -> None:
        super().__init__(parent, app)
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        self.cls = tk.StringVar(value="c-single")
        ttk.Combobox(bar, textvariable=self.cls, values=sorted(CLASSES), width=10,
                     state="readonly").pack(side="left")
        ttk.Button(bar, text="▶ Launch Demo Process", style="Accent.TButton",
                   command=lambda: app.launch(self.cls.get())).pack(side="left", padx=6)
        self.buttons: dict[str, ttk.Button] = {}
        for key, label, style, command in (
                ("checkpoint", "◉ Checkpoint Now", "TButton", self.checkpoint),
                ("stop", "⏹ Stop Process", "Danger.TButton", self.stop),
                ("archive", "Archive Record", "TButton", self.archive),
                ("unarchive", "Unarchive", "TButton", self.unarchive)):
            button = ttk.Button(bar, text=label, style=style, command=command)
            button.pack(side="left", padx=3)
            self.buttons[key] = button
            Tooltip(button, functools.partial(self._reason, key))
        ttk.Button(bar, text="Clear Completed Demo History", style="Danger.TButton",
                   command=app.clear_history).pack(side="right")
        ttk.Button(bar, text="⟳ Refresh", command=app.refresh_now).pack(side="right", padx=4)
        self.show_archived = tk.BooleanVar(value=False)
        ttk.Checkbutton(self, text="Show archived records", variable=self.show_archived,
                        command=app.refresh_now).pack(anchor="w", pady=(0, 6))
        frame, self.tree = make_table(self, ("Workload", "ID", "PID", "State", "Live",
                                             "Last checkpoint", "Last operation"),
                                      (120, 150, 70, 120, 50, 200, 260), height=12)
        frame.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.update_view(app.store))
        ttk.Label(self, text="Details", style="Section.TLabel").pack(anchor="w", pady=(10, 2))
        self.details = text_box(self, 7, app.mode)
        self.details.pack(fill="x")

    def selected(self) -> dict[str, Any] | None:
        sel = self.tree.selection()
        return next((w for w in self.app.store.workloads if w["id"] == (sel[0] if sel else None)),
                    None)

    def _reason(self, key: str) -> str:
        return pr.workload_actions(self.selected())[key].reason

    def update_view(self, store: Store) -> None:
        rows: list[tuple[str, Sequence[str]]] = []
        for w in store.workloads:
            inst = w.get("instance") or {}
            last_ck = w.get("last_checkpoint") or {}
            last_op = w.get("last_operation") or {}
            rows.append((str(w["id"]), (
                str(w["workload_class"]) + (" (archived)" if w.get("archived") else ""),
                str(w["id"]), str(inst.get("pid") or pr.NA), pr.display_state(w),
                "yes" if w.get("live") else "no",
                f"gen {last_ck['generation']} {pr.age(last_ck.get('created_utc'))}"
                if last_ck else "none",
                f"{last_op.get('kind')} {last_op.get('state')}"
                + (f" ({last_op.get('error_code')})" if last_op.get("error_code") else "")
                if last_op else "none")))
        fill(self.tree, rows)
        chosen = self.selected()
        actions = pr.workload_actions(chosen)
        for key, button in self.buttons.items():
            button.state(["!disabled"] if actions[key].enabled else ["disabled"])
        if chosen is None:
            set_text(self.details, "Select a workload to see its details.")
            return
        w = chosen
        state = pr.display_state(w)
        progress = w.get("progress") or {}
        lines = [f"{state}: {pr.STATE_INFO.get(state, ('', ''))[1]}",
                 f"Command: {' '.join(w['argv'][:1])} ({w['workload_class']})",
                 "Live progress: " + (", ".join(f"{role} counter {v['counter']} (pid {v['kpid']})"
                                               for role, v in progress.items()) or pr.NA)]
        last_op = w.get("last_operation") or {}
        if last_op.get("error_code"):
            lines.append(f"Last error: {last_op['error_code']}: {last_op.get('message')}")
            lines.append(f"Suggested action: {pr.suggested_action(last_op['error_code'])}")
        set_text(self.details, "\n".join(lines))

    def checkpoint(self) -> None:
        w = self.selected()
        if w:
            self.app.run_op("Checkpoint", "checkpoint.request", workload_id=w["id"])

    def stop(self) -> None:
        w = self.selected()
        if w and messagebox.askyesno("Stop process", f"Terminate {w['id']}?\n\nIts newest "
                                     "checkpoint stays restorable.", parent=self):
            self.app.run_op("Stop", "workloads.stop", workload_id=w["id"])

    def archive(self) -> None:
        w = self.selected()
        if w:
            self.app.run_op("Archive", "history.archive", workload_id=w["id"], archived=True)

    def unarchive(self) -> None:
        w = self.selected()
        if w:
            self.app.run_op("Unarchive", "history.archive", workload_id=w["id"], archived=False)


# ---------------------------------------------------------------- checkpoints


class CheckpointsPage(Page):
    title = "Checkpoints"
    subtitle = ("Checkpoint history. PUBLISHED: newest complete checkpoint of an instance. "
                "SUPERSEDED: overwritten by a newer one (history only). INVALID: integrity "
                "failed (never restored). MISSING: files gone.")

    def __init__(self, parent: tk.Misc, app: App) -> None:
        super().__init__(parent, app)
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        for label, style, command in (("Verify Integrity", "TButton", self.verify),
                                      ("View Details", "TButton", self.details),
                                      ("⟲ Restore", "Accent.TButton", self.restore),
                                      ("Archive", "TButton", self.archive),
                                      ("Delete Checkpoint", "Danger.TButton", self.delete),
                                      ("⟳ Refresh", "TButton", app.refresh_now)):
            ttk.Button(bar, text=label, style=style, command=command).pack(side="left", padx=3)
        self.show_archived = tk.BooleanVar(value=False)
        ttk.Checkbutton(self, text="Show archived checkpoints", variable=self.show_archived,
                        command=app.refresh_now).pack(anchor="w", pady=(0, 6))
        frame, self.tree = make_table(self, ("Checkpoint", "Workload", "Gen", "Created", "Size",
                                             "State", "Integrity", "Restore", "History"),
                                      (130, 130, 40, 170, 80, 100, 160, 260, 140), height=14)
        frame.pack(fill="both", expand=True)

    def selected(self) -> dict[str, Any] | None:
        sel = self.tree.selection()
        return next((c for c in self.app.store.checkpoints
                     if c["id"] == (sel[0] if sel else None)), None)

    def update_view(self, store: Store) -> None:
        fill(self.tree, [(str(c["id"]), (
            str(c["id"]), str(c["workload_id"]), str(c["generation"]),
            pr.short_time(c["created_utc"]), pr.human_bytes(c.get("total_bytes")),
            pr.checkpoint_label(c) + (" (archived)" if c.get("archived") else ""),
            pr.integrity_text(c),
            ("✔ " if c.get("restore_eligible") else "✖ ") + str(c.get("eligibility_reason")),
            pr.restore_history_text(c))) for c in store.checkpoints])

    def _need(self) -> dict[str, Any] | None:
        c = self.selected()
        if c is None:
            messagebox.showinfo("NapGuard", "Select a checkpoint first.", parent=self)
        return c

    def verify(self) -> None:
        c = self._need()
        if c:
            self.app.run_op("Verify", "checkpoints.verify", checkpoint_id=c["id"])

    def details(self) -> None:
        c = self._need()
        if c:
            info = pr.CHECKPOINT_INFO.get(str(c["state"]), ("", "", ""))[2]
            self.app.details_dialog(f"Checkpoint {c['id']}", f"{pr.checkpoint_label(c)}: {info}",
                                    c)

    def restore(self) -> None:
        c = self._need()
        if c:
            self.app.confirm_restore(c)

    def archive(self) -> None:
        c = self._need()
        if c:
            self.app.run_op("Archive checkpoint", "checkpoints.archive", checkpoint_id=c["id"],
                            archived=not c.get("archived"))

    def delete(self) -> None:
        c = self._need()
        if c and messagebox.askyesno(
                "Delete checkpoint", f"Permanently delete {c['id']} ({pr.checkpoint_label(c)})?"
                "\n\nIts image files are removed only if it holds the newest image of its "
                "instance. The activity log keeps a record.", icon="warning", parent=self):
            self.app.run_op("Delete checkpoint", "checkpoints.delete", checkpoint_id=c["id"],
                            confirm=True)


# ------------------------------------------------------------------- recovery


class RecoveryPage(Page):
    title = "Recovery"
    subtitle = ("Restore a stopped workload from its newest checkpoint with genuine DMTCP. A "
                "restore succeeds only if the restored process is this restore's own process, "
                "keeps the original nonce, and resumes counting from the checkpoint.")

    def __init__(self, parent: tk.Misc, app: App) -> None:
        super().__init__(parent, app)
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="⟲ Restore Selected", style="Accent.TButton",
                   command=self.restore).pack(side="left")
        ttk.Button(bar, text="★ Start Guided Demo",
                   command=lambda: app.show("Guided Demo")).pack(side="left", padx=6)
        ttk.Button(bar, text="View Technical Details",
                   command=self.technical).pack(side="left")
        ttk.Label(self, text="Restorable checkpoints", style="Section.TLabel").pack(anchor="w")
        frame, self.tree = make_table(self, ("Checkpoint", "Workload", "Gen", "Created", "Size"),
                                      (160, 160, 50, 200, 100), height=7)
        frame.pack(fill="x", pady=(4, 10))
        ttk.Label(self, text="Latest restore outcome", style="Section.TLabel").pack(anchor="w")
        self.outcome = ttk.Label(self, text="No restore yet.", style="Muted.TLabel")
        self.outcome.pack(anchor="w", pady=(2, 4))
        frame2, self.evidence = make_table(self, ("Evidence", "Value"), (260, 520), height=9)
        frame2.pack(fill="both", expand=True)

    def update_view(self, store: Store) -> None:
        fill(self.tree, [(str(c["id"]), (str(c["id"]), str(c["workload_id"]),
                                         str(c["generation"]), pr.short_time(c["created_utc"]),
                                         pr.human_bytes(c.get("total_bytes"))))
                         for c in store.checkpoints if c.get("restore_eligible")])
        restores = [o for o in store.operations if o.get("kind") == "restore"]
        if not restores:
            self.outcome.configure(text="No restore yet.", style="Muted.TLabel")
            fill(self.evidence, [])
            return
        op = restores[0]
        ok = op.get("state") == "SUCCEEDED"
        role = "ok" if ok else "error" if op.get("state") == "FAILED" else "busy"
        text = (f"{op.get('state')} — {op.get('checkpoint_id')} at "
                f"{pr.short_time(op.get('ended_utc') or op.get('created_utc'))}")
        if not ok and op.get("error_code"):
            text += (f"\n{op['error_code']}: {op.get('message')}\nSuggested action: "
                     f"{pr.suggested_action(op['error_code'])}")
        self.outcome.configure(text=text, style=f"{role}.Status.TLabel")
        result = op.get("result") if ok else None
        fill(self.evidence, [(str(i), row) for i, row in enumerate(pr.recovery_rows(result))])

    def restore(self) -> None:
        sel = self.tree.selection()
        c = next((c for c in self.app.store.checkpoints if sel and c["id"] == sel[0]), None)
        if c is None:
            messagebox.showinfo("NapGuard", "Select a restorable checkpoint first.", parent=self)
            return
        self.app.confirm_restore(c)

    def technical(self) -> None:
        restores = [o for o in self.app.store.operations if o.get("kind") == "restore"]
        if restores:
            self.app.details_dialog("Latest restore", "Technical details", restores[0])
        else:
            messagebox.showinfo("NapGuard", "No restore has been attempted yet.", parent=self)


# ----------------------------------------------------------------- automation


class AutomationPage(Page):
    title = "Automation"
    subtitle = ("When enabled, the daemon's scheduler checkpoints every eligible running demo "
                "workload once per interval (at least 60 s). All operations run one at a time, "
                "so a scheduled checkpoint never overlaps a manual one. Restored instances are "
                "not scheduled.")

    def __init__(self, parent: tk.Misc, app: App) -> None:
        super().__init__(parent, app)
        form = ttk.Frame(self)
        form.pack(fill="x")
        self.enabled = tk.BooleanVar(value=False)
        self.interval = tk.IntVar(value=300)
        self.loaded = False
        ttk.Checkbutton(form, text="Enable automatic checkpoints",
                        variable=self.enabled).grid(row=0, column=0, sticky="w")
        ttk.Label(form, text="Interval (seconds, minimum 60):").grid(row=1, column=0, sticky="w",
                                                                    pady=6)
        ttk.Spinbox(form, from_=60, to=86400, increment=30, textvariable=self.interval,
                    width=10).grid(row=1, column=1, sticky="w")
        ttk.Combobox(form, values=("60", "120", "300", "600", "1800"), width=8,
                     state="readonly", textvariable=self.interval).grid(row=1, column=2, padx=6)
        ttk.Button(form, text="Save", style="Accent.TButton", command=self.save).grid(
            row=2, column=0, sticky="w", pady=6)
        ttk.Button(form, text="◉ Checkpoint All Eligible Now",
                   command=self.run_now).grid(row=2, column=1, sticky="w", padx=6)
        self.status = ttk.Label(self, text=pr.NA, style="Muted.TLabel", wraplength=900,
                                justify="left")
        self.status.pack(anchor="w", pady=8)
        ttk.Label(self, text="Scheduled and triggered checkpoint attempts",
                  style="Section.TLabel").pack(anchor="w")
        frame, self.tree = make_table(self, ("When", "Trigger", "Workload", "Result", "Details"),
                                      (170, 170, 150, 110, 400), height=10)
        frame.pack(fill="both", expand=True, pady=(4, 0))

    def update_view(self, store: Store) -> None:
        sched = store.scheduler or {}
        if store.status and not self.loaded:
            s = store.status["settings"]
            self.enabled.set(bool(s["auto_checkpoint_enabled"]))
            self.interval.set(int(s["auto_checkpoint_interval_s"]))
            self.loaded = True
        lines = [f"Scheduler: {'ENABLED' if sched.get('enabled') else 'disabled'}, interval "
                 f"{sched.get('interval_s', pr.NA)} s, checks every {sched.get('tick_s', pr.NA)} s."]
        for e in sched.get("eligible", []):
            lines.append(f" • {e['workload_id']}: next checkpoint "
                         f"{pr.short_time(e.get('next_due_utc')) if e.get('next_due_utc') else 'when enabled'}"
                         f" (last {pr.short_time(e.get('last_checkpoint_utc'))})")
        if not sched.get("eligible"):
            lines.append(" • No eligible running workload (launch a demo first).")
        self.status.configure(text="\n".join(lines))
        fill(self.tree, [(str(o["id"]), (pr.short_time(o.get("created_utc")), str(o.get("trigger")),
                                         str(o.get("workload_id")), str(o.get("state")),
                                         str(o.get("error_code") or o.get("message") or "")))
                         for o in sched.get("recent_automatic", [])])

    def save(self) -> None:
        try:
            interval = int(self.interval.get())
        except (tk.TclError, ValueError):
            interval = 0
        if interval < 60:
            messagebox.showerror("NapGuard", "The interval must be at least 60 seconds.",
                                 parent=self)
            return
        self.app.call("Settings saved", "settings.set",
                      auto_checkpoint_enabled=bool(self.enabled.get()),
                      auto_checkpoint_interval_s=interval)

    def run_now(self) -> None:
        self.app.call("Scheduler pass requested", "scheduler.run_now")


# ------------------------------------------------------------- simulation lab


class SimulationPage(Page):
    title = "Simulation Lab"
    subtitle = ("Classroom demonstrations. Battery readings here are SIMULATED (a VM has no "
                "battery hardware), but every checkpoint they trigger is a REAL DMTCP checkpoint "
                "with its real result. Nothing here powers off or suspends the system.")

    def __init__(self, parent: tk.Misc, app: App) -> None:
        super().__init__(parent, app)
        self.banner = ttk.Label(self, text="", style="warn.Status.TLabel")
        self.banner.pack(anchor="w")
        lab = ttk.Frame(self, style="Card.TFrame", padding=14)
        lab.pack(fill="x", pady=8)
        ttk.Label(lab, text="SIMULATED BATTERY", style="warn.Badge.TLabel").grid(row=0, column=0,
                                                                                sticky="w")
        self.percent = tk.IntVar(value=15)
        self.charging = tk.StringVar(value="discharging")
        ttk.Scale(lab, from_=0, to=100, variable=self.percent, orient="horizontal",
                  length=280).grid(row=1, column=0, pady=6, sticky="w")
        self.pct_label = ttk.Label(lab, text="15%", style="Card.TLabel")
        self.pct_label.grid(row=1, column=1, padx=6)
        self.percent.trace_add("write", lambda *_: self.pct_label.configure(
            text=f"{int(self.percent.get())}%"))
        ttk.Radiobutton(lab, text="Discharging", value="discharging",
                        variable=self.charging).grid(row=1, column=2, padx=6)
        ttk.Radiobutton(lab, text="Charging (AC)", value="charging",
                        variable=self.charging).grid(row=1, column=3, padx=6)
        buttons = ttk.Frame(lab, style="Card.TFrame")
        buttons.grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        for label, style, command in (
                ("Apply SIMULATED Reading", "TButton", self.apply),
                ("⚡ Trigger Simulated Low Battery", "Accent.TButton", self.low),
                ("Simulate Charging / Recovery", "TButton", self.recover),
                ("Reset Simulation", "TButton", self.reset),
                ("◉ Run Scheduler Pass Now", "TButton",
                 lambda: app.call("Scheduler pass requested", "scheduler.run_now")),
                ("★ Termination & Recovery Demo", "TButton",
                 lambda: app.show("Guided Demo"))):
            ttk.Button(buttons, text=label, style=style, command=command).pack(side="left",
                                                                              padx=3)
        self.expect = ttk.Label(self, style="Muted.TLabel", wraplength=1000, justify="left", text=(
            "Expected: a simulated reading at or below the LOW threshold (default 20%) makes the "
            "daemon request one real checkpoint of every eligible running demo workload "
            "(trigger 'low_battery_simulated'); at or below CRITICAL (10%) the trigger is "
            "'critical_battery_simulated'. Each fires once per discharge; charging, or 5% above "
            "the threshold, re-arms it. If no workload is running, nothing is checkpointed."))
        self.expect.pack(anchor="w", pady=(4, 8))
        ttk.Label(self, text="Event timeline", style="Section.TLabel").pack(anchor="w")
        frame, self.tree = make_table(self, ("Time", "Kind", "What happened"),
                                      (170, 220, 640), height=11)
        frame.pack(fill="both", expand=True, pady=(4, 0))

    def apply(self) -> None:
        self.app.call("SIMULATED reading applied", "battery.simulate",
                      percent=int(self.percent.get()),
                      ac_online=self.charging.get() == "charging")

    def low(self) -> None:
        low = int(((self.app.store.status or {}).get("settings") or {}).get(
            "battery_low_percent", 20))
        self.percent.set(max(0, low - 1))
        self.charging.set("discharging")
        self.apply()

    def recover(self) -> None:
        self.percent.set(80)
        self.charging.set("charging")
        self.apply()

    def reset(self) -> None:
        self.app.call("Simulation cleared", "battery.simulate", clear=True)

    def update_view(self, store: Store) -> None:
        b = (store.status or {}).get("battery") or {}
        self.banner.configure(text=pr.battery_long(b))
        rows: list[tuple[str, tuple[str, ...]]] = []
        for e in store.events:
            if str(e["kind"]).startswith(("battery.", "scheduler.")):
                rows.append((f"e{e['seq']}", (pr.short_time(e["ts_utc"]), str(e["kind"]),
                                              pr.event_text(e))))
        for o in store.operations:
            trigger = str(o.get("trigger") or "")
            if o.get("kind") == "checkpoint" and ("battery" in trigger or "scheduler" in trigger
                                                  or trigger == "periodic"):
                rows.append((f"o{o['id']}", (pr.short_time(o.get("created_utc")),
                                             f"REAL checkpoint ({trigger})",
                                             f"{o.get('state')} for {o.get('workload_id')}"
                                             + (f": {o.get('error_code')}" if o.get("error_code")
                                                else ""))))
        rows.sort(key=lambda r: r[1][0], reverse=True)
        fill(self.tree, rows[:60])


# ------------------------------------------------------------------- activity


class ActivityPage(Page):
    title = "Activity"
    subtitle = "Everything the daemon recorded, newest first. Simulated events are labelled."

    def __init__(self, parent: tk.Misc, app: App) -> None:
        super().__init__(parent, app)
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        self.level = tk.StringVar(value="All")
        for level in ("All", "Success", "Warning", "Error"):
            ttk.Radiobutton(bar, text=level, value=level, variable=self.level,
                            command=lambda: self.update_view(app.store)).pack(side="left", padx=3)
        self.search = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.search, width=28)
        entry.pack(side="left", padx=10)
        entry.bind("<KeyRelease>", lambda _e: self.update_view(app.store))
        ttk.Button(bar, text="View Technical Details", command=self.technical).pack(side="left",
                                                                                    padx=3)
        ttk.Button(bar, text="Copy Details", command=self.copy).pack(side="left", padx=3)
        ttk.Button(bar, text="Export Diagnostics", command=self.export).pack(side="left", padx=3)
        frame, self.tree = make_table(self, ("Time", "Event", "Workload", "Status", "Details"),
                                      (160, 190, 140, 80, 560), height=18)
        frame.pack(fill="both", expand=True)

    def update_view(self, store: Store) -> None:
        fill(self.tree, [(str(e["seq"]), (pr.short_time(e["ts_utc"]), str(e["kind"]),
                                          pr.event_workload(e), pr.severity(e), pr.event_text(e)))
                         for e in store.events
                         if pr.matches(e, self.level.get(), self.search.get().strip())])

    def selected(self) -> dict[str, Any] | None:
        sel = self.tree.selection()
        return next((e for e in self.app.store.events if sel and str(e["seq"]) == sel[0]), None)

    def technical(self) -> None:
        e = self.selected()
        if e:
            self.app.details_dialog(f"Event {e['seq']}", pr.event_text(e), e)

    def copy(self) -> None:
        e = self.selected()
        if e:
            self.clipboard_clear()
            self.clipboard_append(json.dumps(e, indent=2, sort_keys=True, default=str))
            self.app.set_status("Event details copied to the clipboard.")

    def export(self) -> None:
        def done(result: Any) -> None:
            messagebox.showinfo("Diagnostics exported", f"Written to:\n{result['path']}\n"
                                f"({pr.human_bytes(result['bytes'])}, readable only by you)",
                                parent=self)

        self.app.worker.submit(lambda: self.app.client.call("diagnostics.export"), done,
                               self.app.show_error)


# ----------------------------------------------------------- settings & help

HOW_TO = """How to use NapGuard

1. Start NapGuard. Open the app (napguard gui). If the banner says the daemon is not running,
   press "Start daemon (developer mode)" and read the acknowledgement: the DMTCP coordinator
   is unauthenticated, so this is for a single-user VM only.
2. Launch a demo workload: Processes > Launch Demo Process (c-single or c-threads).
3. Create a checkpoint: select it and press Checkpoint Now (or Dashboard > Create
   Checkpoint). DMTCP saves the process's real execution state.
4. Stop the workload: Processes > Stop Process. Its newest checkpoint stays restorable.
5. Verify and restore: Checkpoints > Verify Integrity, then Restore (or use Recovery). The
   restore is accepted only if the restored process is ours, keeps the original nonce, and
   resumes counting from the checkpoint. External files are not rolled back.
6. Scheduling: Automation > enable, choose an interval (60 s minimum), Save.
7. Battery simulation: Simulation Lab > Trigger Simulated Low Battery. The reading is
   SIMULATED; the checkpoint it triggers is real.
8. Archive old records: Processes > select a finished workload > Archive Record. Show them
   again with "Show archived records".
9. Reset a demo safely: stop every workload, then Settings & Help > Reset Demo. A preview
   lists exactly what will be deleted; the activity log is kept.
10. Exit: close the window to leave the daemon running, or use "Stop Daemon & Exit" to stop
    the daemon (it stops its demo workloads first).

Guided Demo: Dashboard > Start Guided Demo walks through all seven steps with the real
backend and shows the recovery evidence (PIDs, nonces, counters)."""


class SettingsPage(Page):
    title = "Settings & Help"
    subtitle = "Configuration, environment, and a beginner's guide."

    def __init__(self, parent: tk.Misc, app: App) -> None:
        super().__init__(parent, app)
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="Toggle Light/Dark Theme", command=app.toggle_theme).pack(
            side="left", padx=3)
        ttk.Button(bar, text="Show Welcome Again",
                   command=lambda: app.welcome(force=True)).pack(side="left", padx=3)
        ttk.Button(bar, text="Reset Demo…", style="Danger.TButton",
                   command=app.reset_demo).pack(side="left", padx=3)
        ttk.Button(bar, text="Stop Daemon & Exit", style="Danger.TButton",
                   command=app.stop_daemon_and_exit).pack(side="right")
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.info_box = text_box(body, 22, app.mode)
        self.info_box.pack(side="left", fill="both", expand=True, padx=(0, 8))
        guide = text_box(body, 22, app.mode)
        guide.pack(side="left", fill="both", expand=True)
        set_text(guide, HOW_TO)

    def update_view(self, store: Store) -> None:
        s = store.status
        if s is None:
            set_text(self.info_box, f"NapGuard {__version__}\nDaemon: not reachable at "
                     f"{self.app.sock_path}\n\nStart it from the banner, or in a terminal:\n"
                     "  napguard daemon --i-accept-single-user-coordinator")
            return
        b = s.get("backend") or {}
        st = s.get("settings") or {}
        lines = [f"Application version: {__version__}",
                 f"Daemon: running (pid {s.get('pid')}), production authorized: no",
                 f"Developer mode: {s.get('coordinator_mode')}",
                 f"Backend: {b.get('name')} {b.get('version') or pr.NA}, verified="
                 f"{b.get('verified')}, source {b.get('source_commit') or pr.NA}",
                 f"Data directory: {s.get('data_dir')}", f"Socket: {s.get('socket')}",
                 f"Automatic checkpoints: {'on' if st.get('auto_checkpoint_enabled') else 'off'},"
                 f" every {st.get('auto_checkpoint_interval_s')} s",
                 f"Battery thresholds: low {st.get('battery_low_percent')}%, critical "
                 f"{st.get('battery_critical_percent')}%",
                 f"Battery support: {pr.battery_long(s.get('battery') or {})}",
                 f"Supported workload classes: {', '.join(s.get('supported_classes', []))}",
                 "", "Known limitations:", *[f" • {x}" for x in pr.LIMITATIONS]]
        set_text(self.info_box, "\n".join(lines))


# ---------------------------------------------------------------- guided demo


class GuidedDemoPage(Page):
    title = "Guided Demo"
    subtitle = ("Seven steps with the REAL backend: launch, observe, checkpoint, stop, verify, "
                "restore, and verify recovery. A step runs only after the previous one passed; a "
                "failed integrity or security check is never skipped.")

    def __init__(self, parent: tk.Misc, app: App) -> None:
        super().__init__(parent, app)
        self.demo = pr.GuidedDemo()
        self.worker = Worker(app.root)  # independent of the refresh worker
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        self.next_btn = ttk.Button(bar, text="Run Next Step", style="Accent.TButton",
                                   command=self.run_next)
        self.next_btn.pack(side="left")
        self.all_btn = ttk.Button(bar, text="Run All Remaining", command=self.run_all)
        self.all_btn.pack(side="left", padx=6)
        ttk.Button(bar, text="Restart Demo", command=self.restart).pack(side="left")
        self.progress = ttk.Progressbar(self, maximum=len(pr.STEPS), length=500)
        self.progress.pack(anchor="w", pady=(0, 10))
        self.rows: list[tuple[ttk.Label, ttk.Label, ttk.Label]] = []
        steps = ttk.Frame(self)
        steps.pack(fill="x")
        for i, title in enumerate(pr.STEPS):
            frame = ttk.Frame(steps, style="Card.TFrame", padding=8)
            frame.pack(fill="x", pady=2)
            num = ttk.Label(frame, text=f"STEP {i + 1}", style="CardMuted.TLabel", width=8)
            num.pack(side="left")
            ttk.Label(frame, text=title, style="Card.TLabel", width=20).pack(side="left")
            status = ttk.Label(frame, text="Pending", style="muted.Badge.TLabel", width=10)
            status.pack(side="left")
            detail = ttk.Label(frame, text="", style="Card.TLabel", wraplength=640)
            detail.pack(side="left", padx=8)
            self.rows.append((num, status, detail))
        ttk.Label(self, text="Recovery evidence (as reported by the daemon)",
                  style="Section.TLabel").pack(anchor="w", pady=(10, 2))
        frame2, self.evidence = make_table(self, ("Evidence", "Value"), (260, 520), height=9)
        frame2.pack(fill="both", expand=True)
        self.auto = False
        self.render()

    def render(self) -> None:
        roles = {"Pending": "muted", "Running": "busy", "Passed": "ok", "Failed": "error",
                 "Blocked": "warn"}
        for step, (_num, status, detail) in zip(self.demo.steps, self.rows, strict=True):
            status.configure(text=step.status, style=f"{roles[step.status]}.Badge.TLabel")
            detail.configure(text=step.detail)
        self.progress.configure(value=sum(1 for s in self.demo.steps if s.status == "Passed"))
        nxt = self.demo.next_index()
        running = any(s.status == "Running" for s in self.demo.steps)
        state = ["!disabled"] if nxt is not None and not running else ["disabled"]
        self.next_btn.state(state)
        self.all_btn.state(state)
        fill(self.evidence, [(str(i), row) for i, row in
                             enumerate(pr.recovery_rows(self.demo.values.get("restore")))])

    def restart(self) -> None:
        if any(s.status == "Running" for s in self.demo.steps):
            return
        self.demo = pr.GuidedDemo()
        self.render()

    def run_all(self) -> None:
        self.auto = True
        self.run_next()

    def run_next(self) -> None:
        i = self.demo.next_index()
        if i is None:
            self.auto = False
            return
        self.demo.start(i)
        self.render()
        self.worker.submit(lambda: self._step(i), lambda r: self._done(i, r),
                           lambda e: self._failed(i, e))

    def _op(self, method: str, **params: Any) -> dict[str, Any]:
        accepted = self.app.client.call(method, **params)
        return wait_operation(self.app.client.call, accepted["operation_id"])

    def _step(self, i: int) -> tuple[bool, str]:
        v = self.demo.values
        client = self.app.client
        if i == 0:
            op = self._op("workloads.launch_demo", workload_class="c-single")
            if op["state"] != "SUCCEEDED":
                return False, f"{op.get('error_code')}: {op.get('message')}"
            v["workload_id"] = op["result"]["workload_id"]
            return True, f"Launched {v['workload_id']}, pid {op['result']['pid']}, nonce " \
                         f"{op['result']['nonce'][:12]}…"
        wid = v["workload_id"]
        if i == 1:
            def reading() -> dict[str, Any]:
                w = next(w for w in client.call("workloads.list") if w["id"] == wid)
                return dict((w.get("progress") or {}).get("main") or {})

            first = reading()
            time.sleep(2.0)
            second = reading()
            if not first or not second:
                return False, "No progress reports from the workload."
            ok = int(second["counter"]) > int(first["counter"])
            return ok, (f"pid {second.get('kpid')}: counter {first['counter']} → "
                        f"{second['counter']} in 2 s" + ("" if ok else " (NOT advancing)"))
        if i == 2:
            op = self._op("checkpoint.request", workload_id=wid)
            if op["state"] != "SUCCEEDED":
                return False, f"{op.get('error_code')}: {op.get('message')}"
            v["checkpoint_id"] = op["result"]["checkpoint_id"]
            return True, (f"Checkpoint {v['checkpoint_id']}: "
                          f"{pr.human_bytes(op['result']['bytes'])} in "
                          f"{op['result']['capture_ms']} ms (DMTCP)")
        if i == 3:
            time.sleep(2.0)  # let the original advance past the checkpoint
            op = self._op("workloads.stop", workload_id=wid)
            return op["state"] == "SUCCEEDED", ("Original terminated and confirmed gone."
                                                if op["state"] == "SUCCEEDED" else
                                                f"{op.get('error_code')}: {op.get('message')}")
        if i == 4:
            op = self._op("checkpoints.verify", checkpoint_id=v["checkpoint_id"])
            return op["state"] == "SUCCEEDED", ("Manifest, catalog digest and image hashes "
                                                "verified." if op["state"] == "SUCCEEDED" else
                                                f"{op.get('error_code')}: {op.get('message')}")
        if i == 5:
            op = self._op("recovery.restore", checkpoint_id=v["checkpoint_id"], confirm=True)
            if op["state"] != "SUCCEEDED":
                v["restore_error"] = op
                return False, (f"{op.get('error_code')}: {op.get('message')} — "
                               f"{pr.suggested_action(op.get('error_code'))}")
            v["restore"] = op["result"]
            return True, f"DMTCP restored the process as pid {op['result'].get('restored_pid')}."
        result = v.get("restore") or {}
        ok = result.get("continuity_verified") is True and \
            result.get("original_nonce") == result.get("restored_nonce")
        return ok, ("Continuity verified: same nonce, counter rolled back to the checkpoint "
                    "window and advancing." if ok else "Continuity could NOT be verified.")

    def _done(self, i: int, result: tuple[bool, str]) -> None:
        ok, detail = result
        self.demo.finish(i, ok, detail)
        self.render()
        self.app.refresh_now()
        if ok and self.auto:
            self.run_next()
        else:
            self.auto = False

    def _failed(self, i: int, exc: IpcError) -> None:
        self.demo.finish(i, False, f"{exc.code}: {exc.message} — {pr.suggested_action(exc.code)}")
        self.auto = False
        self.render()


# ---------------------------------------------------------------------- shell


class App:
    def __init__(self, root: tk.Tk, sock_path: Path, *, cfg_path: Path | None = None,
                 smoke: bool = False) -> None:
        self.root = root
        self.sock_path = sock_path
        self.client = Client(sock_path, timeout=30)
        self.cfg_path = cfg_path or config_path()
        self.config = load_config(self.cfg_path)
        if smoke:  # no modal dialogs and no config writes in a smoke run
            self.config = {"welcomed": True, "close_notice_shown": True}
        self.mode = "light" if self.config.get("theme") == "light" else "dark"
        theme.apply(root, self.mode)
        self.store = Store()
        self.worker = Worker(root)
        self.errors: list[str] = []
        self._fetching = False
        root.title("NapGuard — Intelligent Process Recovery")
        root.geometry("1280x800")
        root.minsize(980, 620)
        self._icon()
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.show("Dashboard")
        self.refresh()
        if not self.config.get("welcomed"):
            root.after(400, self.welcome)

    def _icon(self) -> None:
        img = tk.PhotoImage(width=32, height=32)
        for y in range(32):
            for x in range(32):
                d = ((x - 15.5) ** 2 + (y - 15.5) ** 2) ** 0.5
                if d < 15:
                    img.put("#22d3ee" if d > 11 or (abs(x - 15.5) < 3 and 7 < y < 25) else
                            "#0b1426", (x, y))
        self._icon_img = img
        self.root.iconphoto(True, img)

    def _build(self) -> None:
        header = ttk.Frame(self.root, style="Header.TFrame", padding=(18, 10))
        header.pack(fill="x")
        ttk.Label(header, text="◉ NapGuard", style="Brand.TLabel").pack(side="left")
        ttk.Label(header, text="  Intelligent Process Recovery", style="Tagline.TLabel").pack(
            side="left")
        self.conn = ttk.Label(header, text="Connecting…", style="muted.Header.TLabel")
        self.conn.pack(side="right")
        self.mode_label = ttk.Label(header, text="", style="warn.Header.TLabel")
        self.mode_label.pack(side="right", padx=16)
        self.banner = ttk.Frame(self.root, style="Panel.TFrame", padding=(18, 6))
        self.banner_text = ttk.Label(self.banner, text="", style="error.Header.TLabel")
        self.banner_text.pack(side="left")
        self.banner_btn = ttk.Button(self.banner, text="Start daemon (developer mode)",
                                     style="Accent.TButton", command=self.start_daemon)
        self.banner_btn.pack(side="right")
        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True)
        side = ttk.Frame(body, style="Sidebar.TFrame", padding=(0, 10))
        side.pack(side="left", fill="y")
        self.nav: dict[str, ttk.Button] = {}
        for name in (*PAGES[:4], "Guided Demo", *PAGES[4:]):
            b = ttk.Button(side, text=f"  {ICONS[name]}  {name}", style="Nav.TButton", width=22,
                           command=functools.partial(self.show, name))
            b.pack(fill="x")
            self.nav[name] = b
        ttk.Label(side, text=f"v{__version__} · developer build", style="Panel.TLabel",
                  foreground=theme.colour(self.mode, "muted")).pack(side="bottom", pady=8)
        self.content = ttk.Frame(body)
        self.content.pack(side="left", fill="both", expand=True)
        classes: dict[str, type[Page]] = {
            "Dashboard": DashboardPage, "Processes": ProcessesPage,
            "Checkpoints": CheckpointsPage, "Recovery": RecoveryPage,
            "Guided Demo": GuidedDemoPage, "Automation": AutomationPage,
            "Simulation Lab": SimulationPage, "Activity": ActivityPage,
            "Settings & Help": SettingsPage}
        self.pages = {name: cls(self.content, self) for name, cls in classes.items()}
        self.statusbar = ttk.Label(self.root, text="", style="Muted.TLabel", padding=(18, 4))
        self.statusbar.pack(fill="x")
        self.current = "Dashboard"

    # --------------------------------------------------------------- navigation

    def show(self, name: str) -> None:
        for page in self.pages.values():
            page.pack_forget()
        self.pages[name].pack(fill="both", expand=True)
        for key, button in self.nav.items():
            button.configure(style="NavActive.TButton" if key == name else "Nav.TButton")
        self.current = name
        self.pages[name].update_view(self.store)

    def set_status(self, text: str) -> None:
        self.statusbar.configure(text=text)

    # ------------------------------------------------------------------ refresh

    def refresh(self) -> None:
        self.refresh_now()
        self.root.after(REFRESH_MS, self.refresh)

    def refresh_now(self) -> None:
        if self._fetching:
            return
        self._fetching = True
        procs, ckpts = self.pages["Processes"], self.pages["Checkpoints"]
        assert isinstance(procs, ProcessesPage) and isinstance(ckpts, CheckpointsPage)
        archived_w, archived_c = bool(procs.show_archived.get()), bool(ckpts.show_archived.get())

        def fetch() -> dict[str, Any]:
            c = self.client
            return {"status": c.call("daemon.status"),
                    "workloads": c.call("workloads.list", include_archived=archived_w),
                    "checkpoints": c.call("checkpoints.list", include_archived=archived_c),
                    "events": c.call("events.list", limit=300),
                    "scheduler": c.call("scheduler.status"),
                    "operations": c.call("operations.list", limit=60)}

        self.worker.submit(fetch, self._fetched, self._offline)

    def _fetched(self, data: dict[str, Any]) -> None:
        self._fetching = False
        s = self.store
        s.connected, s.error = True, None
        s.status, s.workloads, s.checkpoints = data["status"], data["workloads"], data["checkpoints"]
        s.events, s.scheduler, s.operations = data["events"], data["scheduler"], data["operations"]
        developer = "developer" in str(s.status.get("coordinator_mode", ""))
        self.conn.configure(text="● Connected", style="ok.Header.TLabel")
        self.mode_label.configure(text="⚠ DEVELOPER MODE — single-user VM only, not production"
                                  if developer else "⚠ Coordinators refused (restart the daemon "
                                  "in developer mode to checkpoint)")
        self.banner.pack_forget()
        self.pages[self.current].update_view(s)

    def _offline(self, exc: IpcError) -> None:
        self._fetching = False
        self.store.connected, self.store.error, self.store.status = False, exc, None
        self.conn.configure(text="● Daemon not running", style="error.Header.TLabel")
        self.mode_label.configure(text="")
        self.banner_text.configure(text=f"The NapGuard daemon is not reachable ({exc.code}).")
        self.banner.pack(fill="x", after=self.root.winfo_children()[0])
        self.pages[self.current].update_view(self.store)

    # --------------------------------------------------------------- operations

    def show_error(self, exc: IpcError) -> None:
        self.error_dialog(exc.code, exc.message, exc.details)

    def call(self, label: str, method: str, **params: Any) -> None:
        def done(_result: Any) -> None:
            self.set_status(f"{label}.")
            self.refresh_now()

        self.worker.submit(lambda: self.client.call(method, **params), done, self.show_error)

    def run_op(self, label: str, method: str, **params: Any) -> None:
        """Submit an operation; report success only when the daemon confirms it."""
        self.set_status(f"{label}: requested, waiting for the daemon…")

        def run() -> dict[str, Any]:
            accepted = self.client.call(method, **params)
            return wait_operation(self.client.call, accepted["operation_id"])

        def done(op: dict[str, Any]) -> None:
            self.refresh_now()
            if op.get("state") == "SUCCEEDED":
                self.set_status(f"{label}: succeeded in {op.get('duration_ms')} ms.")
            else:
                self.set_status(f"{label}: {op.get('state')} ({op.get('error_code')}).")
                self.error_dialog(str(op.get("error_code")), str(op.get("message")), op)

        self.worker.submit(run, done, self.show_error)

    def launch(self, workload_class: str) -> None:
        self.run_op(f"Launch {workload_class}", "workloads.launch_demo",
                    workload_class=workload_class)

    def checkpoint_first_running(self) -> None:
        running = [w for w in self.store.workloads
                   if pr.workload_actions(w)["checkpoint"].enabled]
        if not running:
            messagebox.showinfo("NapGuard", "No running demo workload can be checkpointed. "
                                "Launch one first.", parent=self.root)
            return
        self.run_op("Checkpoint", "checkpoint.request", workload_id=running[0]["id"])

    def confirm_restore(self, c: dict[str, Any]) -> None:
        if not c.get("restore_eligible"):
            messagebox.showwarning("Not restorable", f"{c['id']} cannot be restored now:\n"
                                   f"{c.get('eligibility_reason')}", parent=self.root)
            return
        if messagebox.askyesno(
                "Restore checkpoint",
                f"Restore {c['id']} (generation {c['generation']}) of {c['workload_id']}?\n\n"
                f"Created: {pr.short_time(c['created_utc'])} ({pr.age(c['created_utc'])})\n"
                f"Backend: {c['backend']} {c['backend_version']}\n\n"
                "The process resumes from the checkpoint. Files and other external state are "
                "NOT rolled back (LIM-007). The restore is accepted only after ownership, "
                "nonce and counter-continuity checks pass.", parent=self.root):
            self.run_op("Restore", "recovery.restore", checkpoint_id=c["id"], confirm=True)

    def _preview_and_delete(self, title: str, ids: list[str] | None, method: str,
                            **params: Any) -> None:
        def show(preview: dict[str, Any]) -> None:
            eligible, blocked = preview["eligible"], preview["blocked"]
            lines = [f"{e['workload_id']}: {e['instances']} instance(s), {e['checkpoints']} "
                     f"checkpoint(s), {e['files']} file(s), {pr.human_bytes(e['bytes'])}\n"
                     f"    {e['directory']}" for e in eligible]
            lines += [f"KEPT {b['workload_id']}: {b['reason']}" for b in blocked]
            if method == "history.reset_demo" and blocked:
                messagebox.showwarning(title, "Reset refused: stop every workload first.\n\n"
                                       + "\n".join(lines), parent=self.root)
                return
            if not eligible:
                messagebox.showinfo(title, "Nothing to delete.\n\n" + "\n".join(lines),
                                    parent=self.root)
                return
            if messagebox.askyesno(
                    title, "PERMANENTLY delete the following? This cannot be undone.\n\n"
                    + "\n".join(lines[:20]) + f"\n\nTotal: {preview['total_files']} file(s), "
                    f"{pr.human_bytes(preview['total_bytes'])}. {preview['kept']}.",
                    icon="warning", parent=self.root):
                extra = dict(params)
                if method == "history.delete":
                    extra["workload_ids"] = [e["workload_id"] for e in eligible]
                self.run_op(title, method, confirm=True, **extra)

        self.worker.submit(lambda: self.client.call("history.preview", **(
            {"workload_ids": ids} if ids is not None else {})), show, self.show_error)

    def clear_history(self) -> None:
        self._preview_and_delete("Clear completed demo history", None, "history.delete")

    def reset_demo(self) -> None:
        self._preview_and_delete("Reset demo", None, "history.reset_demo")

    # ------------------------------------------------------------------ dialogs

    def error_dialog(self, code: str, message: str, details: Any) -> None:
        win = tk.Toplevel(self.root)
        win.title(f"NapGuard — {code}")
        win.configure(bg=theme.colour(self.mode, "bg"))
        ttk.Label(win, text=code, style="error.Status.TLabel", padding=(16, 12, 16, 0)).pack(
            anchor="w")
        ttk.Label(win, text=message, wraplength=560, justify="left", padding=(16, 6)).pack(
            anchor="w")
        ttk.Label(win, text="Suggested action: " + pr.suggested_action(code), wraplength=560,
                  justify="left", style="Muted.TLabel", padding=(16, 0, 16, 8)).pack(anchor="w")
        box = text_box(win, 12, self.mode)
        text = json.dumps(details, indent=2, sort_keys=True, default=str)

        def toggle() -> None:
            if box.winfo_ismapped():
                box.pack_forget()
            else:
                box.pack(fill="both", expand=True, padx=16)
                set_text(box, text)

        bar = ttk.Frame(win, padding=12)
        bar.pack(fill="x", side="bottom")
        ttk.Button(bar, text="View Technical Details", command=toggle).pack(side="left")

        def copy() -> None:
            win.clipboard_clear()
            win.clipboard_append(f"{code}: {message}\n{text}")

        ttk.Button(bar, text="Copy Error Details", command=copy).pack(side="left", padx=6)
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="right")

    def details_dialog(self, title: str, summary: str, data: Any) -> None:
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=theme.colour(self.mode, "bg"))
        ttk.Label(win, text=summary, wraplength=640, justify="left", padding=12).pack(anchor="w")
        box = text_box(win, 22, self.mode)
        box.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        set_text(box, json.dumps(data, indent=2, sort_keys=True, default=str))

    def welcome(self, force: bool = False) -> None:
        if self.config.get("welcomed") and not force:
            return
        messagebox.showinfo(
            "Welcome to NapGuard",
            "NapGuard saves the real execution state of a running process with DMTCP, and "
            "restores it later, verifying that it resumed instead of starting again.\n\n"
            "Try Dashboard > Start Guided Demo for a 7-step walkthrough.\n\n"
            "This is a developer build for a single-user Ubuntu VM: the DMTCP coordinator is "
            "unauthenticated and only NapGuard's reference workloads are supported.",
            parent=self.root)
        self.config["welcomed"] = True
        save_config(self.cfg_path, self.config)

    def toggle_theme(self) -> None:
        self.config["theme"] = "light" if self.mode == "dark" else "dark"
        save_config(self.cfg_path, self.config)
        messagebox.showinfo("Theme", "The new theme applies the next time NapGuard opens.",
                            parent=self.root)

    # -------------------------------------------------------------- daemon life

    def start_daemon(self) -> None:
        if not messagebox.askyesno(
                "Start daemon in developer mode",
                "Start the NapGuard daemon with --i-accept-single-user-coordinator?\n\n"
                "The DMTCP coordinator has no authentication. NapGuard confines it to loopback "
                "and refuses multi-user hosts, but this is acceptable ONLY on a single-user "
                "development VM (ADR 0006/0008). This acknowledgement applies to this daemon "
                "run only and is not stored.", icon="warning", parent=self.root):
            return
        package_root = Path(__file__).resolve().parents[2]
        env = dict(os.environ, PYTHONPATH=str(package_root))
        log_dir = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") \
            / "napguard"
        log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        log = open(log_dir / "daemon-gui.log", "ab")  # noqa: SIM115 - handed to the child
        try:
            subprocess.Popen([sys.executable, "-s", "-m", "napguard.main", "--socket",
                              str(self.sock_path), "daemon", "--i-accept-single-user-coordinator"],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                             start_new_session=True, close_fds=True)
        finally:
            log.close()
        self.set_status("Daemon starting (developer mode)…")
        self.root.after(1500, self.refresh_now)

    def stop_daemon_and_exit(self) -> None:
        if not self.store.connected:
            self.root.destroy()
            return
        if messagebox.askyesno("Stop daemon & exit", "Stop the NapGuard daemon? It stops its "
                               "demo workloads first. Checkpoints and history are kept.",
                               parent=self.root):
            self.worker.submit(lambda: self.client.call("daemon.shutdown", confirm=True),
                               lambda _r: self.root.destroy(), self.show_error)

    def on_close(self) -> None:
        if not self.config.get("close_notice_shown"):
            messagebox.showinfo("NapGuard", "Closing the window does NOT stop the daemon or your "
                                "workloads (UX-004). To stop everything, use Settings & Help > "
                                "Stop Daemon & Exit.", parent=self.root)
            self.config["close_notice_shown"] = True
            save_config(self.cfg_path, self.config)
        self.root.destroy()


def main(socket_path: str, smoke: bool = False) -> int:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Cannot open a window ({exc}); run from the Ubuntu desktop session.",
              file=sys.stderr)
        return 2
    errors: list[str] = []
    def record(*exc: Any) -> None:
        errors.append("".join(traceback.format_exception(*exc)))

    root.report_callback_exception = record
    cfg = Path(os.environ.get("NAPGUARD_GUI_CONFIG", str(config_path())))
    app = App(root, Path(socket_path), cfg_path=cfg, smoke=smoke)
    if smoke:
        for name in app.pages:
            app.show(name)
            root.update()
        # Wait for a REAL answer from the daemon (a completed status/list round trip), not
        # a fixed delay; "connected" is reported only if that answer arrived.
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and not (app.store.connected and app.store.status):
            root.update()
            time.sleep(0.05)
        for name in app.pages:  # render every screen again, now with live data
            app.show(name)
            root.update()
        connected = app.store.connected and app.store.status is not None
        root.destroy()
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        print(f"GUI smoke test: {len(app.pages)} screens rendered; daemon "
              f"{'connected' if connected else 'NOT reachable'}")
        return 0
    root.mainloop()
    if errors:
        print("\n".join(errors), file=sys.stderr)
    return 0
