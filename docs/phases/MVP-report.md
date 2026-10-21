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
