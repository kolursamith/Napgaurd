# NapGuard MVP Report: Stages 04–10 (Developer Demo)

- Date: 2026-10-21
- Branch: `stage-03/backend-integration` (the owner's current development branch)
- Base: Stage 03a, **verified on Ubuntu ARM64** at `b12ca96d5a3cecc69ea15ed393cf3153d0da7cd6` (owner-reported: 143 Linux tests passed; genuine DMTCP checkpoint and restore with continuity; no exposure; no leftovers)
- Decision record: [ADR 0008](../decisions/0008-mvp-developer-demo-integration.md) (**Proposed**: owner acceptance requested)
- Status: **IMPLEMENTED; MAC-TESTED ONLY; AWAITING UBUNTU VERIFICATION.** Nothing in Stages 04–10 has run on Ubuntu yet. The MVP is developer-only and **not production-authorized**.

## Implemented

| Stage | What | Where |
|---|---|---|
| 04 Storage | SQLite catalog: WAL, `synchronous=FULL`, foreign keys, one transaction per change. Workloads, instances (PID, start ticks, UID, boot ID), checkpoint history (generation, trigger, state, manifest SHA-256, size, timing, continuity marks), operations, activity log, settings. Startup reconciliation: interrupted operations, lost instances, missing checkpoints | `storage/catalog.py` |
| 05 Supervisor | Launch the verified reference workloads (`c-single`, `c-threads`) through the Stage 03a adapter; monitor; stop by process group and coordinator; exited instances detected. External processes are never attached, and unsupported classes are refused | `daemon/manager.py` |
| 06 Daemon and IPC | User-level daemon over a 0700/0600 Unix socket. `flock`; `SO_PEERCRED` (other UIDs get `UNAUTHENTICATED`, which is audited); 4-byte-length JSON frames ≤ 1 MiB; strict per-method schemas; no TCP | `daemon/ipc.py`, `daemon/service.py` |
| 06 CLI | `napguard daemon/status/launch-demo/workloads/checkpoint/stop/checkpoints/verify/restore/schedule/battery/events/gui` | `main.py` |
| 07 Scheduler | Automatic checkpoints, off by default, interval ≥ 60 s, through a single operation worker. No overlapping operations; duplicate requests are merged | `daemon/scheduler.py`, `daemon/ops.py` |
| 08 Battery | sysfs readings (several batteries and AC), unavailable when absent (typical in UTM), low/critical triggers with hysteresis, and **labelled SIMULATED** readings for demos | `power/battery.py` |
| 09 Recovery | Strict integrity verification (catalog manifest digest, manifest schema, artifact hashes → `CORRUPT`); genuine DMTCP restore; FR-025 conflict check; FR-026 window; nonce and counter continuity. Anything else is `FAILED` | `daemon/manager.py` |
| 10 GUI | Tkinter, with background workers for every daemon call. Tabs: Dashboard, Managed processes (Launch Demo Process, Checkpoint Now, Stop), Checkpoint history (Verify Integrity, Restore Checkpoint with a LIM-007 warning), Automatic checkpoints, Battery (SIMULATED controls clearly labelled), Activity log | `ui/gui.py`, `ui/view.py` |

The verified Stage 03a path is unchanged: DMTCP 4.2.0 adapter, fail-closed coordinator, completion wait, manifest, and artifact checks.

## Tests (Mac, macOS arm64, no DMTCP)

New MVP tests (labelled TEST DOUBLE backend in `tests/unit/mvp_fakes.py`; real files, real SQLite, real Unix sockets):

- `test_mvp_catalog_manager.py`:
  - pragmas and reconciliation;
  - launch, checkpoint history and supersession, FR-025 conflict, verified restore;
  - no checkpoint of a restored instance;
  - fresh start is not a restore;
  - tampered image or manifest gives `CORRUPT` and is refused;
  - class restriction and coordinator refusal; early exit.
- `test_mvp_scheduler_battery.py`:
  - due logic and the 60 s minimum; off by default;
  - merging and no overlap;
  - sysfs parsing, unavailable battery, simulated labelling, hysteresis.
- `test_mvp_daemon_ipc.py`:
  - the end-to-end flow over IPC;
  - socket, data, and catalog permissions; single-instance lock;
  - other-UID rejection;
  - oversized, garbled, and wrong-version frames; unknown methods and fields; types; confirmation; settings validation;
  - refusal without acknowledgement;
  - scheduler and simulated battery triggers;
  - the CLI against the daemon and with the daemon down;
  - view formatting;
  - real `SO_PEERCRED` (Linux-only).

**Not run on the Mac:** Linux-only tests (real `/proc`, `ss`, `SO_PEERCRED`), every real DMTCP operation, and the GUI window, which is not opened on the development Mac.

## Ubuntu installation and verification (owner-run)

```bash
git fetch origin && git checkout stage-03/backend-integration && git pull --ff-only
sudo apt install python3-tk          # only for the GUI; your explicit decision
bash scripts/mvp-ubuntu-check.sh "$(git rev-parse HEAD)"              # prerequisites only
bash scripts/mvp-ubuntu-check.sh "$(git rev-parse HEAD)" --run-demo   # one real end-to-end demo
```

**GUI:** start the daemon in one terminal, then open the GUI in another, from the repository root:

```bash
PYTHONPATH=src python3 -m napguard.main daemon --i-accept-single-user-coordinator
PYTHONPATH=src python3 -m napguard.main gui
```

## Known limitations

- **Developer mode only.** The coordinator is unauthenticated. The single-user check is a heuristic. Not production-authorized (ADR 0006, ADR 0008).
- Only `c-single` and `c-threads` reference workloads. No Python, socket, GUI, or multi-process workloads.
- Only the newest checkpoint generation of an instance is restorable (DMTCP overwrites its image). A restored instance cannot be checkpointed again.
- Stopping the daemon stops its workloads (FR-030 not met). No cold-boot auto-recovery (REL-007), no retention or quotas (REL-010, REL-011), FIFO instead of priorities (FR-016), and no suspend/logind integration (Stage 08 covers battery only).
- External files are not rolled back by a restore (LIM-007).
- Simulated battery readings are for demonstration only; a UTM VM normally has no battery.

## First Ubuntu MVP run (commit `43832c3`): shutdown failure in the verification script

**Observed** (owner-reported):

- 166 Linux tests passed.
- These all passed: genuine DMTCP checkpoint, integrity verification, genuine restore, counter continuity.
- The final step **failed**: "daemon exit 143, socket left behind, process still running".
- A manual `kill -TERM` of the real daemon PID made it exit within 3 s, and it removed its socket.

**Cause (script, not daemon).** The script started the daemon with `ng daemon … &`, and `ng` is a shell function. Backgrounding a function makes bash fork a wrapper subshell that starts Python as its child, so `$!` was the wrapper:

- `kill -TERM $!` killed only the wrapper, which is the exit 143 (128 + 15);
- the daemon never received the signal.

This was reproduced on the Mac with the same pattern.

**Fix:**

- `scripts/lib/daemon-control.sh`. `start_daemon` backgrounds the command itself, so `$!` is the daemon. `stop_daemon`:
  - signals only a PID whose command line still identifies the daemon;
  - waits, with a bound, until the process has actually exited (zombies are not counted as running);
  - collects the daemon's own exit status;
  - requires its socket to be gone.

  It passes only on status 0 with the socket removed. A timeout, a signal, a non-zero status, or a socket left behind is a FAIL.
- The MVP script uses these helpers and also checks that the daemon PID no longer exists.
- No daemon, checkpoint, restore, or security code changed.

**Regression tests** (`tests/unit/test_mvp_shutdown.py`):

- with a fake daemon: `$!` is the daemon, a clean stop passes, a socket left behind fails, a non-zero exit fails, an ignored SIGTERM times out (and is not claimed stopped), and a process that is not ours is never signalled;
- the script starts the daemon directly and stops it only through the helper;
- the real daemon, in a subprocess, exits 0 on SIGTERM, removes its socket, and records its workload as STOPPED.

## GUI restore failure: "VERIFY_FAILED: the reporting process is not this restore's process"

**Observed** (owner-reported). A GUI restore of `ck-c502a9cb82b1` (workload `wl-c5e25adb0309`, generation 2, PUBLISHED) failed the ownership check. The automated Ubuntu runs passed.

**Root cause.** A race between the reference workload's reporting and DMTCP's asynchronous checkpoint. It is not a foreign process.

- `ngcounter.c` `report()` reads its kernel PID, then (for `c-single`) computes the digest of the 4 MiB ballast, composes the line, opens the file, and writes it.
- DMTCP can suspend and checkpoint the process at any instruction. When that happens inside `report()`, the image holds a half-finished report that already contains the **original** process's PID.
- After a restore, the first thing the restored process does is finish that write. Its first line therefore carries the dead original's PID, which no restore can adopt.
- The window is a few milliseconds per ~100 ms report period, wider for `c-single` because of the ballast digest. So it struck occasionally: in a GUI session with several checkpoints, not in the single-checkpoint automated runs.
- The labelled test double reproduces the exact error.

**Fix** (`selftest/verify.py::ownership_problems`, used by the daemon restore and the Stage 03a self-test):

- every restored report must come from a process this restore can adopt: this user, this restore's session, started after the restore began;
- the only exception is the **first** restored line of each role, and only if it carries the original instance's PID and the original is confirmed dead (identity checked);
- **still FAIL:** a second line with the original's PID, or one after an owned line; any other unadoptable PID; a missing PID; a role with no line from this restore's own process.

The ownership problems are recorded in the error detail. The DMTCP adapter, the coordinator, and the audits are unchanged.

**Regression tests:**

- `tests/unit/test_mvp_restore_ownership.py`: the rule, including every failure case; the in-flight restore of generation 2; a stray foreign writer still refused; three checkpoints, then restore, stop, and restore the same checkpoint again, three times;
- `test_a_report_in_flight_at_checkpoint_time_is_not_a_foreign_process` (self-test).

**Confirming the cause on the VM** (read-only, from the existing evidence):

```bash
python3 -I -c "import sqlite3,os; db=sqlite3.connect(os.path.expanduser('~/.local/share/napguard/catalog.db')); [print(r) for r in db.execute(\"select origin,state,pid,kill_line,started_utc from instances where workload_id='wl-c5e25adb0309' order by started_utc\")]"
grep -h adopt_refused ~/.local/share/napguard/workloads/wl-c5e25adb0309/session-restore-*/logs/events.jsonl
```

With this root cause, the refused PID is the LAUNCH instance's PID, refused as "not running", and the first report after the restore began carries that PID.
