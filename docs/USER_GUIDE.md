# NapGuard User Guide

NapGuard saves the **real execution state** of a running process with DMTCP (a checkpoint) and later restores it. It then checks that the process **resumed** rather than started again.

It is a developer build for a single-user Ubuntu 24.04 ARM64 VM. Only NapGuard's own reference workloads (`c-single`, `c-threads`) are supported. Read [Known Limitations](KNOWN_LIMITATIONS.md) first.

## 1. Install and start

1. Build DMTCP 4.2.0 once, as yourself (no root): `bash experiments/stage02/build-dmtcp.sh`.
2. Install the GUI toolkit, your own decision, with `sudo`: `sudo apt install python3-tk`.
3. Install NapGuard for your user (no root):

   ```bash
   bash scripts/install.sh --with-desktop
   ```

   This creates the commands `napguard` and `napguard-dev` in `~/.local/bin`, and an application-grid launcher.
4. Open **NapGuard** from the application grid, or run `napguard gui`.
5. If the red banner says the daemon is not running, press **Start daemon (developer mode)**. Read the acknowledgement: the DMTCP coordinator is unauthenticated, so this is acceptable only on a single-user VM. The acknowledgement applies to that daemon run only. In a terminal you can instead run `napguard daemon --i-accept-single-user-coordinator`.

## 2. The window

```
┌───────────────────────────────────────────────────────────────────────────────┐
│ ◉ NapGuard  Intelligent Process Recovery    ⚠ DEVELOPER MODE   ● Connected     │
├────────────────┬──────────────────────────────────────────────────────────────┤
│ ◈ Dashboard    │  Title and a one-line explanation of the screen               │
│ ▶ Processes    │  ┌──────────┐ ┌──────────┐ ┌──────────┐   status cards          │
│ ◉ Checkpoints  │  └──────────┘ └──────────┘ └──────────┘                         │
│ ⟲ Recovery     │  [ actions ]                                                    │
│ ★ Guided Demo  │  ┌───────────────────────── table ──────────────────────────┐ │
│ ⏱ Automation   │  │                                                          │ │
│ ⚗ Simulation   │  └──────────────────────────────────────────────────────────┘ │
│ ☰ Activity     │  details / evidence                                            │
│ ⚙ Settings     │                                                                │
├────────────────┴──────────────────────────────────────────────────────────────┤
│ status bar: the outcome of the last action, as confirmed by the daemon          │
└───────────────────────────────────────────────────────────────────────────────┘
```

Every action runs in the background, so the window never freezes. A success message appears only after the daemon confirms success. A failure opens a dialog with:

- the error code;
- a suggested action;
- **View Technical Details** and **Copy Error Details**.

## 3. Screens

| Screen | What it shows | Actions |
|---|---|---|
| **Dashboard** | Cards: daemon, DMTCP backend, active processes, saved checkpoints, automatic checkpoints, battery. Also recent activity, latest checkpoint, latest restore, the running operation, and limitations. Unavailable values say "Not available" or "Unavailable", never 0. | Launch Demo Process, Create Checkpoint, Open Recovery, Start Guided Demo |
| **Processes** | Each demo workload: class, ID, PID, state, live, last checkpoint, last operation. Details below the table: what the state means, live counter progress, and the last error with a suggested action. | Launch Demo Process (choose `c-single` or `c-threads`), Checkpoint Now, Stop Process, Archive Record, Unarchive, Refresh, Clear Completed Demo History. Disabled buttons explain why on hover. |
| **Checkpoints** | History: ID, workload, generation, time, size, state, integrity, restore eligibility (✔ or ✖ with the reason), restore history | Verify Integrity, View Details, Restore, Archive, Delete Checkpoint, Refresh |
| **Recovery** | Only the checkpoints that can be restored now, plus the latest restore outcome with its evidence (PIDs, nonces, counters) | Restore Selected, Start Guided Demo, View Technical Details |
| **Guided Demo** | Seven steps with live status (Pending, Running, Passed, Failed, Blocked) and the recovery evidence | Run Next Step, Run All Remaining, Restart Demo |
| **Automation** | Scheduler on or off, interval, each eligible workload's next checkpoint time, and the history of scheduled and triggered attempts | Enable, interval (60 s minimum), Save, Checkpoint All Eligible Now |
| **Simulation Lab** | The battery state (real, or a clearly labelled **SIMULATED** reading), what to expect, and a timeline linking simulated events to the REAL checkpoints they caused | Battery slider, Charging or Discharging, Apply SIMULATED Reading, Trigger Simulated Low Battery, Simulate Charging / Recovery, Reset Simulation, Run Scheduler Pass Now, Termination & Recovery Demo |
| **Activity** | Every daemon event: time, event, workload, status (Success, Warning, Error), details. Simulated events are marked `[SIMULATED]`. | Filter by level, search, View Technical Details, Copy Details, Export Diagnostics |
| **Settings & Help** | Versions, daemon, developer mode, data directory, socket, automation, battery support, supported classes, limitations, and the built-in "How to use NapGuard" guide | Toggle Light/Dark Theme, Show Welcome Again, Reset Demo…, Stop Daemon & Exit |

## 4. Process states

These are derived from the daemon's own records. NapGuard never invents a state.

| State | Meaning |
|---|---|
| STARTING | NapGuard is launching the workload under DMTCP. |
| RUNNING | Running under NapGuard; it can be checkpointed. |
| CHECKPOINTING | A DMTCP checkpoint is in progress. |
| STOPPING | NapGuard is terminating it. |
| STOPPED | Stopped through NapGuard; its newest checkpoint can be restored. |
| RESTORING | DMTCP is restoring it. |
| RESTORED | Running again from a checkpoint, with continuity verified. Not checkpointed again in this version. |
| EXITED | The process ended on its own. |
| FAILED | A launch or restore did not complete or could not be verified. |
| LOST | The daemon restarted while it was running. |

## 5. Checkpoint states

| State | Meaning | Restorable? |
|---|---|---|
| PUBLISHED | Complete, recorded, and the newest generation of its instance | yes, once the workload is stopped |
| SUPERSEDED | A newer checkpoint of the same instance overwrote this image (DMTCP reuses the image file) | no; history only |
| INVALID | Integrity verification failed: the manifest or image changed | never |
| MISSING | Its files are gone | no |

A restore is accepted only if:

- the restored process belongs to this restore (this user, this restore's session, started after the restore began);
- it keeps the **original nonce**;
- its counter **rolled back** to the checkpoint and **advances**.

Anything else is FAILED, with technical details. Files and other external state are **not** rolled back.

## 6. Archive, delete, and reset

| Action | What happens | Reversible |
|---|---|---|
| **Archive Record** | Hides a finished workload from the default list (tick "Show archived records" to see it). Nothing is deleted. An archived PUBLISHED checkpoint stays restorable. | yes (Unarchive) |
| **Delete Checkpoint** | Deletes one checkpoint record. Its image files are deleted only if it holds the newest image of its instance, so a newer generation is never touched. | no |
| **Clear Completed Demo History** | Previews, then permanently deletes every **finished** workload: its catalog rows and its own directory `~/.local/share/napguard/workloads/<id>/`. Running or busy workloads are listed as KEPT. | no |
| **Reset Demo** | Same as clearing all history, and it also restores the default settings and clears a battery simulation. It is refused while anything is running: stop every workload first. | no |

Safety rules:

- The activity log is always **kept**, as the audit record. Every cleanup adds an event naming what was removed.
- Nothing outside a workload's own directory is touched, and symlinks are removed, never followed.
- Cleanup runs on the same single worker as checkpoint and restore, so it can never overlap them.
- If deleting files fails part-way, the checkpoints are marked MISSING first, so they can never look restorable. You can then retry.

## 7. Automation

Enable automatic checkpoints in **Automation** and choose an interval (60 s minimum). The daemon then checkpoints every **running, launched** demo workload once per interval. Operations run one at a time, so a scheduled checkpoint never overlaps a manual one. Restored instances are not scheduled.

## 8. Simulation Lab

A VM has no battery, so the Battery card says **Unavailable**. The Simulation Lab can apply a **SIMULATED** reading, labelled everywhere:

- At or below the low threshold (default 20%), the daemon requests a **real** DMTCP checkpoint of every eligible running workload, with trigger `low_battery_simulated`.
- At or below critical (10%), the trigger is `critical_battery_simulated`.
- Each fires once per discharge. Charging, or rising 5% above the threshold, re-arms it.

The timeline shows each simulated event and the real result of the checkpoint it caused. Nothing here suspends, powers off, or changes system settings.

## 9. Exit

- **Closing the window** leaves the daemon and your workloads running.
- **Settings & Help > Stop Daemon & Exit** stops the daemon. It stops its demo workloads first; checkpoints and history are kept.

## 10. Command line

Every GUI action has a command, for example:

```bash
napguard status
napguard launch-demo --class c-single
napguard checkpoint <workload-id>
napguard stop <workload-id>
napguard verify <checkpoint-id>
napguard restore <checkpoint-id> --yes
napguard schedule --enable --interval 60
napguard battery --simulate 15
napguard archive <workload-id>
napguard history
napguard clear-history --yes
napguard reset-demo --yes
napguard events --limit 50
napguard diagnostics
napguard shutdown --yes
```

## 11. Uninstall

```bash
bash scripts/uninstall.sh
```

This removes the commands, the launcher, the icon, and the unit. Your data is kept.

```bash
bash scripts/uninstall.sh --purge
```

This also deletes `~/.local/share/napguard` and `~/.config/napguard`, after you type `PURGE`.
