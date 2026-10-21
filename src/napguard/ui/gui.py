"""Stage 10 (MVP): the NapGuard desktop GUI (Tkinter), a client of the daemon's IPC.

Every daemon call runs on a background worker thread; results are handed back to the
Tk thread with `after()`, so the window never blocks on a checkpoint or a restore. The
GUI performs no checkpoint itself and shows exactly what the daemon reports, including
failures. Simulated battery readings are labelled as such everywhere.
"""

from __future__ import annotations

import queue
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:  # Ubuntu: `sudo apt install python3-tk`
    tk = None  # type: ignore[assignment]

from napguard.daemon.ipc import Client, IpcError
from napguard.selftest.run import CLASSES

from . import view

REFRESH_MS = 2000


class Worker:
    """Runs daemon calls off the Tk thread, one at a time, in order."""

    def __init__(self, root: tk.Tk, client: Client) -> None:
        self.root = root
        self.client = client
        self.jobs: queue.Queue[tuple[Callable[[], Any], Callable[[Any], None],
                                     Callable[[IpcError], None]]] = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, fn: Callable[[], Any], ok: Callable[[Any], None],
               err: Callable[[IpcError], None]) -> None:
        self.jobs.put((fn, ok, err))

    def _run(self) -> None:
        while True:
            fn, ok, err = self.jobs.get()
            try:
                result = fn()
            except IpcError as exc:
                self.root.after(0, err, exc)
            except Exception as exc:  # connection errors etc.
                self.root.after(0, err, IpcError("INTERNAL", repr(exc)))
            else:
                self.root.after(0, ok, result)


class App:
    def __init__(self, root: tk.Tk, client: Client) -> None:
        self.root = root
        self.client = client
        self.worker = Worker(root, client)
        root.title("NapGuard (developer MVP)")
        root.geometry("1100x650")
        self.status_var = tk.StringVar(value="Connecting to the NapGuard daemon…")
        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True)
        self.dashboard = tk.Text(notebook, height=12, wrap="word")
        notebook.add(self.dashboard, text="Dashboard")
        notebook.add(self._processes(notebook), text="Managed processes")
        notebook.add(self._checkpoints(notebook), text="Checkpoint history")
        notebook.add(self._settings(notebook), text="Automatic checkpoints")
        notebook.add(self._battery(notebook), text="Battery")
        events_frame, self.events = self._table(notebook, view.EVENT_COLUMNS, (180, 200, 700))
        notebook.add(events_frame, text="Activity log")
        ttk.Label(root, textvariable=self.status_var, anchor="w").pack(fill="x")
        self.refresh()

    # -------------------------------------------------------------- widgets

    def _table(self, parent: tk.Misc, columns: tuple[str, ...],
               widths: tuple[int, ...]) -> tuple[ttk.Frame, ttk.Treeview]:
        frame = ttk.Frame(parent)
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=14)
        for col, width in zip(columns, widths, strict=True):
            tree.heading(col, text=col)
            tree.column(col, width=width, anchor="w")
        tree.pack(fill="both", expand=True)
        return frame, tree

    def _processes(self, parent: tk.Misc) -> ttk.Frame:
        frame = ttk.Frame(parent)
        bar = ttk.Frame(frame)
        bar.pack(fill="x")
        self.demo_class = tk.StringVar(value="c-single")
        ttk.Combobox(bar, textvariable=self.demo_class, values=sorted(CLASSES), width=10,
                     state="readonly").pack(side="left", padx=4, pady=4)
        ttk.Button(bar, text="Launch Demo Process", command=self.launch).pack(side="left")
        ttk.Button(bar, text="Checkpoint Now", command=self.checkpoint).pack(side="left", padx=4)
        ttk.Button(bar, text="Stop", command=self.stop).pack(side="left")
        table, self.workloads = self._table(frame, view.WORKLOAD_COLUMNS,
                                            (160, 90, 50, 90, 90, 80))
        table.pack(fill="both", expand=True)
        return frame

    def _checkpoints(self, parent: tk.Misc) -> ttk.Frame:
        frame = ttk.Frame(parent)
        bar = ttk.Frame(frame)
        bar.pack(fill="x")
        ttk.Button(bar, text="Verify Integrity", command=self.verify).pack(side="left", padx=4,
                                                                           pady=4)
        ttk.Button(bar, text="Restore Checkpoint", command=self.restore).pack(side="left")
        ttk.Label(bar, text="Only the newest PUBLISHED generation of a stopped workload can be "
                  "restored.").pack(side="left", padx=8)
        table, self.checkpoints = self._table(frame, view.CHECKPOINT_COLUMNS,
                                              (150, 150, 40, 90, 120, 80, 160, 80))
        table.pack(fill="both", expand=True)
        return frame

    def _settings(self, parent: tk.Misc) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=12)
        self.auto_enabled = tk.BooleanVar(value=False)
        self.auto_interval = tk.IntVar(value=300)
        ttk.Checkbutton(frame, text="Enable automatic checkpoints",
                        variable=self.auto_enabled).grid(row=0, column=0, sticky="w")
        ttk.Label(frame, text="Interval (seconds, at least 60):").grid(row=1, column=0, sticky="w")
        ttk.Spinbox(frame, from_=60, to=86400, increment=30, textvariable=self.auto_interval,
                    width=10).grid(row=1, column=1, sticky="w")
        ttk.Button(frame, text="Save", command=self.save_settings).grid(row=2, column=0, pady=8,
                                                                        sticky="w")
        return frame

    def _battery(self, parent: tk.Misc) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=12)
        self.battery_var = tk.StringVar(value="Battery: …")
        ttk.Label(frame, textvariable=self.battery_var).grid(row=0, column=0, columnspan=4,
                                                            sticky="w")
        sim = ttk.LabelFrame(frame, text="SIMULATED battery — demonstration only, never a real "
                             "reading", padding=8)
        sim.grid(row=1, column=0, columnspan=4, sticky="we", pady=12)
        self.sim_percent = tk.IntVar(value=15)
        self.sim_ac = tk.BooleanVar(value=False)
        ttk.Scale(sim, from_=0, to=100, variable=self.sim_percent, orient="horizontal",
                  length=240).grid(row=0, column=0)
        ttk.Label(sim, textvariable=self.sim_percent).grid(row=0, column=1, padx=6)
        ttk.Checkbutton(sim, text="AC online", variable=self.sim_ac).grid(row=0, column=2)
        ttk.Button(sim, text="Apply SIMULATED reading", command=self.simulate).grid(row=0, column=3,
                                                                                   padx=6)
        ttk.Button(sim, text="Clear simulation", command=self.clear_simulation).grid(row=0,
                                                                                     column=4)
        return frame

    # ------------------------------------------------------------- refresh

    def refresh(self) -> None:
        def fetch() -> dict[str, Any]:
            c = self.client
            return {"status": c.call("daemon.status"), "workloads": c.call("workloads.list"),
                    "checkpoints": c.call("checkpoints.list"),
                    "events": c.call("events.list", limit=200)}

        self.worker.submit(fetch, self._show, self._offline)
        self.root.after(REFRESH_MS, self.refresh)

    def _fill(self, tree: ttk.Treeview, rows: list[tuple[str, ...]]) -> None:
        selected = tree.selection()
        tree.delete(*tree.get_children())
        for row in rows:
            tree.insert("", "end", iid=row[0] if tree is not self.events else None, values=row)
        keep = [s for s in selected if tree.exists(s)]
        if keep:
            tree.selection_set(keep)

    def _show(self, data: dict[str, Any]) -> None:
        status = data["status"]
        self.dashboard.delete("1.0", "end")
        self.dashboard.insert("end", "\n".join(view.dashboard_lines(status)))
        self._fill(self.workloads, [view.workload_row(w) for w in data["workloads"]])
        self._fill(self.checkpoints, [view.checkpoint_row(c) for c in data["checkpoints"]])
        self._fill(self.events, [view.event_row(e) for e in data["events"]])
        self.battery_var.set(view.battery_text(status["battery"]))
        if not self.status_var.get().startswith("Working"):
            self.status_var.set("Connected. " + status["coordinator_mode"])

    def _offline(self, exc: IpcError) -> None:
        self.status_var.set(f"Daemon unavailable: {exc.code}: {exc.message}")

    # ------------------------------------------------------------- actions

    def _selected(self, tree: ttk.Treeview, what: str) -> str | None:
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("NapGuard", f"Select a {what} first.")
            return None
        return str(sel[0])

    def _operation(self, label: str, method: str, **params: Any) -> None:
        self.status_var.set(f"Working: {label}…")

        def run() -> dict[str, Any]:
            from napguard.daemon.service import wait_operation

            accepted = self.client.call(method, **params)
            return wait_operation(self.client.call, accepted["operation_id"])

        def done(op: dict[str, Any]) -> None:
            self.status_var.set(view.operation_text(op))
            if op.get("state") != "SUCCEEDED":
                messagebox.showerror("NapGuard", view.operation_text(op))

        def failed(exc: IpcError) -> None:
            self.status_var.set(f"{label} refused: {exc.code}")
            messagebox.showerror("NapGuard", f"{exc.code}: {exc.message}")

        self.worker.submit(run, done, failed)

    def _error(self, exc: IpcError) -> None:
        messagebox.showerror("NapGuard", f"{exc.code}: {exc.message}")

    def launch(self) -> None:
        self._operation("launch demo", "workloads.launch_demo", workload_class=self.demo_class.get())

    def checkpoint(self) -> None:
        wid = self._selected(self.workloads, "workload")
        if wid:
            self._operation("checkpoint", "checkpoint.request", workload_id=wid)

    def stop(self) -> None:
        wid = self._selected(self.workloads, "workload")
        if wid and messagebox.askyesno("NapGuard", f"Stop {wid}? Its process will be terminated."):
            self._operation("stop", "workloads.stop", workload_id=wid)

    def verify(self) -> None:
        cid = self._selected(self.checkpoints, "checkpoint")
        if cid:
            self._operation("verify", "checkpoints.verify", checkpoint_id=cid)

    def restore(self) -> None:
        cid = self._selected(self.checkpoints, "checkpoint")
        if cid and messagebox.askyesno(
                "Restore checkpoint",
                f"Restore {cid} with DMTCP?\n\nThe restored process resumes from the checkpoint. "
                "Files and other external state are NOT rolled back (LIM-007)."):
            self._operation("restore", "recovery.restore", checkpoint_id=cid, confirm=True)

    def save_settings(self) -> None:
        def run() -> Any:
            return self.client.call("settings.set",
                                    auto_checkpoint_enabled=bool(self.auto_enabled.get()),
                                    auto_checkpoint_interval_s=int(self.auto_interval.get()))

        self.worker.submit(run, lambda s: self.status_var.set("Settings saved"),
                           self._error)

    def simulate(self) -> None:
        pct, ac = int(self.sim_percent.get()), bool(self.sim_ac.get())
        self.worker.submit(lambda: self.client.call("battery.simulate", percent=pct, ac_online=ac),
                           lambda b: self.battery_var.set(view.battery_text(b)),
                           self._error)

    def clear_simulation(self) -> None:
        self.worker.submit(lambda: self.client.call("battery.simulate", clear=True),
                           lambda b: self.battery_var.set(view.battery_text(b)),
                           self._error)


def main(socket_path: str) -> int:
    if tk is None:
        print("The GUI needs Tkinter. On Ubuntu: sudo apt install python3-tk", file=sys.stderr)
        return 2
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Cannot open a window ({exc}); run from the Ubuntu desktop session.",
              file=sys.stderr)
        return 2
    App(root, Client(Path(socket_path)))
    root.mainloop()
    return 0
