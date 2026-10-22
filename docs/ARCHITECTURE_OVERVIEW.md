# NapGuard Architecture Overview

```
 ┌──────────────┐   ┌──────────────┐
 │  Tkinter GUI │   │ napguard CLI │        (any number of clients, this user only)
 └──────┬───────┘   └──────┬───────┘
        │  Unix socket $XDG_RUNTIME_DIR/napguard/daemon.sock  (0600, SO_PEERCRED, JSON frames)
 ┌──────▼───────────────────▼─────────────────────────────────────────────────┐
 │                      napguard daemon (ordinary user)                      │
 │  IPC server ─► request validation ─► Executor (ONE worker: no overlaps)   │
 │                                         │                                 │
 │   Scheduler ──(periodic)──────────────► │ ◄──(low/critical)── Battery     │
 │                                         ▼                    monitor      │
 │                           Manager (supervise, checkpoint, verify, restore)│
 │                           History (archive, preview, delete, reset)       │
 │                                         │                                 │
 │   SQLite catalog (WAL, synchronous=FULL) ◄──── every state change         │
 └─────────────────────────────────────────┼─────────────────────────────────┘
                                           ▼
                Stage 03a DMTCP adapter (verified on Ubuntu ARM64)
       policy (default deny) · loopback-only coordinator · audits · completion wait
       · manifest + SHA-256 · process identity · restore ownership and continuity
                                           ▼
                        DMTCP 4.2.0 (pinned build, user-level)
```

## Components

| Component | Code | Role |
|---|---|---|
| GUI | `src/napguard/ui/app.py`, `presenter.py`, `theme.py` | Tkinter/ttk client with a custom dark-navy theme. Background workers make the daemon calls; `presenter.py` holds every display decision as testable pure functions. |
| CLI | `src/napguard/main.py` | The same operations from a terminal; also starts the daemon. |
| Daemon and IPC | `daemon/service.py`, `daemon/ipc.py` | User-level process, 0700/0600 socket, kernel peer-UID check, length-prefixed JSON (1 MiB maximum), strict per-method schemas, no TCP. |
| Executor | `daemon/ops.py` | One worker thread runs every state-changing operation (launch, stop, checkpoint, verify, restore, archive, delete, reset), so none overlap. Requests return an operation ID; duplicate checkpoint requests are merged. |
| Manager | `daemon/manager.py` | Launches only verified reference workloads, stops them, checkpoints them (manifest plus catalog digest), verifies, and restores. A restore is accepted only after ownership, nonce, and counter-continuity checks. |
| History | `daemon/history.py` | Archive, preview, delete, and reset under explicit safety rules (see the [User Guide](USER_GUIDE.md#6-archive-delete-and-reset)). |
| Catalog | `storage/catalog.py` | SQLite: workloads, instances (PID, start time, UID, boot ID), checkpoints, operations, events, settings. Additive migrations; startup reconciliation. |
| Scheduler | `daemon/scheduler.py` | Periodic checkpoints (60 s minimum) through the executor. |
| Battery | `power/battery.py` | sysfs readings while awake; labelled SIMULATED readings; low/critical triggers with hysteresis. |
| Backend | `checkpoint/dmtcp.py` and friends | The Stage 03a adapter, unchanged in principle since its Ubuntu verification ([ADR 0006](decisions/0006-stage03-dmtcp-backend-integration-and-coordinator-security.md)). |

## One checkpoint, end to end

1. The GUI sends `checkpoint.request`. The daemon validates it and queues an operation, returning its ID.
2. The executor runs it:
   - the manager calls the adapter, which re-checks the workload (single process, no sockets, not Python, no devices, no GUI toolkit) and audits the network;
   - DMTCP writes the image;
   - the adapter waits for verified completion: restart script, a stable image, peers RUNNING.
3. The manager writes a manifest (SHA-256 of every file, versions, and what is *not* rolled back). It records the manifest's digest in the catalog, marks the previous generation SUPERSEDED, and logs an event.
4. The GUI polls the operation and shows success only now.

## One restore, end to end

1. Preconditions: the checkpoint is PUBLISHED, the workload is stopped, and nothing is queued.
2. Verification: the catalog digest of the manifest, the strict manifest, and every artifact hash.
3. The adapter starts a fresh loopback-only coordinator and runs `dmtcp_restart` (never the generated shell script).
4. The manager waits for the restored reports and checks three things:
   - **ownership**: every report comes from this restore's process; the one exception is a report that was half-written at checkpoint time;
   - the process stays alive through the verification window;
   - **continuity**: same nonce, counter inside the checkpoint window, advancing.

   Any failure stops the restored process and records FAILED.

Decisions: [ADR 0006](decisions/0006-stage03-dmtcp-backend-integration-and-coordinator-security.md) (coordinator security), [ADR 0007](decisions/0007-synchronous-backend-calls-in-stage-03a.md) (synchronous calls), [ADR 0008](decisions/0008-mvp-developer-demo-integration.md) (MVP integration).
