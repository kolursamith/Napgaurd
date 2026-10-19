# NapGuard Component Architecture and Contracts

- Status: Stage 01 design, not implemented
- Related:
  - [overview](overview.md)
  - [data and state models](data-model.md)
  - [backend contract](checkpoint-backend.md)
  - [storage and recovery](storage-recovery.md)
  - [power and sleep](power-sleep.md)
  - [IPC and security](ipc-security.md)
  - [requirements](../requirements/requirements.md)

```text
PySide6 GUI  /  napguard CLI
      |
      |  authenticated Unix-domain socket (length-prefixed JSON)
      |
User-level systemd daemon (napguard.service, one asyncio event loop)
  +-- Workload registry
  +-- Process supervisor
  +-- Compatibility manager
  +-- Checkpoint manager
  +-- Scheduler
  +-- Power and sleep monitor
  +-- Storage manager
  +-- Recovery manager
  +-- Event logger
  |
  +-- Verified DMTCP/CRIU backend adapter   (selected in Stage 02)
  +-- SQLite metadata                        (catalog.sqlite3)
  +-- Persistent checkpoint artifacts        ($XDG_DATA_HOME/napguard/checkpoints)
```

## Component index

The traceability matrix uses the **Key** column.

| Key | Component | Planned package | Owns persistent records | First stage |
|---|---|---|---|---|
| `backend` | Checkpoint backend adapter | `napguard.checkpoint.dmtcp` / `.criu` | none (writes artifact files into the directory it is given) | 02 (experiments), 03 |
| `compat` | Compatibility manager | `napguard.checkpoint.compatibility` | the compatibility fields of `Workload`, written through the registry | 03 |
| `ckpt` | Checkpoint manager | `napguard.checkpoint.manager` | `CheckpointOperation` | 03 |
| `storage` | Storage manager | `napguard.storage` | `Checkpoint` rows, manifests, artifact directories, schema version | 04 |
| `registry` | Workload registry | `napguard.processes.registry` | `Workload`, `ProtectionPolicy` | 05 |
| `supervisor` | Process supervisor | `napguard.processes.supervisor` | `ProcessInstance`, per-instance stdout/stderr logs | 05 |
| `daemon` | Daemon service core | `napguard.daemon.service` | daemon lock file, `config.toml` (reads it) | 06 |
| `ipc` | IPC server and protocol | `napguard.daemon.ipc` | socket file (runtime only) | 06 |
| `events` | Event logger | `napguard.core.events` | `SystemEvent`, JSON log stream | 06 |
| `cli` | Command-line client | `napguard.cli` | none | 03 (developer CLI), 06 (IPC CLI) |
| `scheduler` | Scheduler | `napguard.scheduler` | none (state rebuilt from policies and operations) | 07 |
| `power` | Power and sleep monitor | `napguard.power`, `napguard.sleep` | none (emits `SystemEvent`s) | 08 |
| `recovery` | Recovery manager | `napguard.recovery` | `RecoveryAttempt` | 09 |
| `gui` | Desktop GUI | `napguard.ui` | GUI preferences only (`$XDG_CONFIG_HOME/napguard/gui.toml`) | 10 |
| `packaging` | Packaging and installation | `packaging/` | installed unit and desktop files | 11 |
| `docs` | Documentation and reports | `docs/` | — | 00–12 |
| `all` | Cross-cutting (all components) | — | — | 00–12 |

Package names follow the target layout in the master architecture document. Each package is created only in the stage that implements it. Packages that already exist from Layer 0 (`checkpoint`, `core`, `daemon`, `power`, `recovery`, `sleep`, `storage`, `ui`) stay empty until then.

## Concurrency model

- The daemon runs one asyncio event loop. Component methods called from IPC handlers are coroutines.
- **Synchronous** (answered within the request): registry CRUD, list and get queries, `daemon.status`, `daemon.capabilities`, policy reads and writes, and `operation.get`. Target: under 200 ms.
- **Asynchronous** (the request returns `ACCEPTED` with an operation ID, and completion arrives as an event or through `operation.get`): `workload.start`, `workload.stop`, `checkpoint.request`, `recovery.restore`, `workload.assess`, `checkpoint.delete`, and `checkpoint.verify`.
- Backend tools run as child processes via `asyncio.create_subprocess_exec`, each with a deadline.
- SQLite is accessed through one writer connection owned by the storage manager, on a dedicated thread. Callers await it. There is exactly one writer, so all catalog writes happen in order.
- Hashing and fsync of large artifacts run in a thread pool so they never block the event loop.
- Each workload has an `asyncio.Lock`. Checkpoint, restore, start, stop, and remove all take it, which makes those operations mutually exclusive per workload.

## Component contracts

### Workload registry (`registry`)

- **Responsibility:** create, validate, update, disable, and remove workload definitions and their protection policies, keyed by stable workload IDs.
- **Inputs:**
  - `register(spec)` and `update(id, patch, expected_config_version)`
  - `remove(id, purge: bool)`
  - `get(id)` and `list()`
  - `set_policy(id, policy)`
  - `set_compatibility(id, report)`, called only by `compat`
- **Outputs:** `Workload` and `ProtectionPolicy` value objects, or a typed error (`INVALID_ARGUMENT`, `NOT_FOUND`, `CONFLICT`).
- **Depends on:** `storage` (catalog transactions) and `events`.
- **Failure modes:**
  - Validation failure, which leaves no state change (FR-002).
  - Concurrent update, rejected by an optimistic `config_version` check.
  - Removal while the workload is active, returning `CONFLICT` (FR-003).
  - Catalog write failure, which surfaces as `STORAGE_ERROR` with nothing partially written.
- **Security boundary:**
  - Validates every path and argument (FR-002, SEC-004).
  - Applies the environment allow-list (SEC-008).
  - Never resolves executables through `PATH`.

### Process supervisor (`supervisor`)

- **Responsibility:** launch, track, stop, and classify managed processes, and own the `ProcessInstance` records.
- **Inputs:**
  - `start(workload_id)` and `restart(workload_id)` (FR-028)
  - `stop(workload_id, grace_s)`
  - `adopt_restored(workload_id, restored_session)`, called by `recovery`
  - `reconcile()` at startup
- **Outputs:**
  - `ProcessInstance` records.
  - Exit events: `process.exited` with class `COMPLETED`, `STOPPED`, `CRASHED`, or `LOST`.
- **Depends on:** `backend` (`launch`, `status`), `storage`, `events`, and `registry` (reads only).
- **Failure modes:**
  - The backend launch fails (`LAUNCH_FAILED`).
  - The process exits before it is registered.
  - PID reuse, defended against by comparing PID, start time, and boot ID (SEC-003).
  - Signal delivery to a process that no longer exists (`PROCESS_GONE`).
  - The daemon restarts while workloads are running (FR-030; see "Process containment" below).
- **Security boundary:**
  - Signals only processes it launched or adopted, using pidfds (SEC-003).
  - Launches with an argument array, `start_new_session=True`, a closed fd set, stdin from `/dev/null`, size-capped 0600 log files, and the daemon's umask of 077 (SEC-004).
- **Sync/async:** start and stop are asynchronous. Exit detection uses pidfd readability (`loop.add_reader` on `os.pidfd_open`), with a 1 s poll as fallback.

### Compatibility manager (`compat`)

- **Responsibility:**
  - Combine the backend probe, the workload spec, and the rules learned in Stage 02 into a `CompatibilityReport`.
  - Promote a workload to `VERIFIED` only on evidence (FR-005, FR-006, CMP-003, CMP-006).
- **Inputs:** `assess(workload)`, `on_recovery_succeeded(attempt)`, and `on_backend_changed(capabilities)`.
- **Outputs:** `CompatibilityReport {status, backend, backend_version, arch, reasons: [code], checked_at}`.
- **Depends on:** `backend` (`probe`, `assess`), `registry` (writes the compatibility fields), and `events`.
- **Failure modes:**
  - The probe fails or times out, giving `UNKNOWN` with reason `PROBE_FAILED`.
  - The backend is missing, giving `UNSUPPORTED` with reason `BACKEND_UNAVAILABLE`.
  - The rules are out of date. They are versioned, and changing them triggers reassessment.
- **Security boundary:** it inspects the executable read-only: ELF header, setuid bit, static linking, and device dependencies. It never executes the workload in order to assess it.

### Checkpoint manager (`ckpt`)

- **Responsibility:**
  - Run checkpoint operations end to end: lock, preflight, backend capture into staging, hand-off to storage for validation and publication, and the final state.
  - Own the `CheckpointOperation` records.
- **Inputs:**
  - `request(workload_id, trigger, priority, deadline)`, called by `scheduler`. Manual IPC requests also go through the scheduler queue.
  - `cancel(operation_id)`
  - `reconcile()` at startup, which marks active operations `INTERRUPTED` (REL-009).
- **Outputs:** the operation state and, on success, the published checkpoint ID. Emits `checkpoint.*` events.
- **Depends on:** `backend` (`checkpoint`, `cancel`, `cleanup`), `storage` (`create_staging`, `publish`, `discard_staging`, space check), `supervisor` (the live session), and `events`.
- **Failure modes:**
  - The workload is not running (`NOT_RUNNING`).
  - Insufficient space (`INSUFFICIENT_SPACE`, REL-010).
  - Backend error (`CHECKPOINT_FAILED`).
  - The deadline passes (`TIMED_OUT`, REL-008).
  - Cancellation (`CANCELLED`).
  - Validation failure (`INVALID_ARTIFACTS`).
  - The daemon crashes partway (`INTERRUPTED`).
  - In every case the staging directory is removed and the previous checkpoint is left untouched (REL-004).
- **Security boundary:** passes the backend only a staging directory created with mode 0700, never a path chosen by the user.

### Scheduler (`scheduler`)

- **Responsibility:**
  - Decide when to checkpoint: periodic timers, manual requests, battery triggers, and pre-suspend.
  - Merge duplicate requests, order by priority, and enforce the global concurrency bound (FR-013 to FR-016).
- **Inputs:**
  - Policy changes from `registry`.
  - `manual_request(workload_id)` from IPC.
  - `power.low`, `power.critical`, and `sleep.prepare` events from `power`.
  - Operation completions from `ckpt`.
- **Outputs:** calls to `ckpt.request`. Pre-suspend batch results go back to `power`.
- **Depends on:** `ckpt`, `registry`, and `events`.
- **Failure modes:**
  - Clock jumps. Timers use the monotonic clock, and wall-clock time is used only for display.
  - A checkpoint storm. It is bounded by per-workload merging, the global bound, and a minimum interval.
  - Starvation. Periodic requests age into a higher priority after 3 skipped intervals.
- **Sync/async:** event-driven, with one scheduling task. Its state is rebuilt at startup from policies and recent operations, so it owns no persistent records.

### Power and sleep monitor (`power`)

- **Responsibility:**
  - Read battery state from sysfs while the system is awake.
  - Turn thresholds into triggers.
  - Hold the logind delay inhibitor and run the pre-suspend protocol ([power and sleep design](power-sleep.md)).
- **Inputs:**
  - A sysfs provider: real, or a simulated provider that is labeled as such.
  - logind D-Bus signals.
  - Policy thresholds.
- **Outputs:**
  - `power.sample`, `power.low`, and `power.critical` events.
  - `sleep.prepare` and `sleep.resumed` events.
  - Capability reports (`battery: present|absent`, `sleep: available|unavailable`).
- **Depends on:** `scheduler`, `events`, and a D-Bus library (chosen in Stage 08 by ADR).
- **Failure modes:**
  - No battery (FR-019).
  - Unreadable or garbled sysfs values: the sample is dropped and an event is logged.
  - logind unavailable (PLT-004).
  - The inhibitor cannot be acquired: pre-suspend checkpoints are disabled and this is reported.
  - The budget is exceeded: the inhibitor is released anyway (FR-020).
- **Security boundary:** reads only and needs no root (PLT-005). Simulated providers mark every event `simulated = true` (UX-005).

### Storage manager (`storage`)

- **Responsibility:**
  - Own the catalog schema and migrations, checkpoint directories, staging, validation, atomic publication, integrity checks, retention, quotas, and startup reconciliation of artifacts ([storage and recovery design](storage-recovery.md)).
- **Inputs:**
  - `create_staging(workload_id, operation_id)`
  - `publish(staging, backend_result)`
  - `discard_staging(path)`
  - `verify(checkpoint_id)`
  - `list_checkpoints(workload_id)`
  - `apply_retention(workload_id)`
  - `check_space(estimate)`
  - `reconcile()`
  - Catalog transactions for the other components
- **Outputs:** `Checkpoint` records, manifests, and integrity results.
- **Depends on:** the filesystem, `sqlite3` (standard library), and `events`.
- **Failure modes:**
  - ENOSPC, EIO, or a failed fsync: the operation fails, staging is discarded, and the previous checkpoint is kept.
  - Corrupt artifacts become `CORRUPT` and are moved to `quarantine/`.
  - Missing artifacts become `MISSING`.
  - The catalog's integrity check fails at startup: the daemon refuses to serve and reports `CATALOG_CORRUPT`. It never silently re-creates the catalog.
- **Security boundary:**
  - Directories 0700 and files 0600.
  - No symlink following; artifacts are opened with `O_NOFOLLOW` relative to the directory fd.
  - Manifest paths are checked (SEC-006, SEC-011).
  - The data directory must not be on tmpfs (PLT-007).

### Recovery manager (`recovery`)

- **Responsibility:**
  - List recovery candidates.
  - Run prechecks: trust, integrity, architecture, machine, backend version, and duplicate checks.
  - Restore through the backend, verify the result, and own the `RecoveryAttempt` records (FR-022 to FR-027).
- **Inputs:** `candidates(workload_id?)`, `restore(workload_id, checkpoint_id, confirm, mode)`, `reconcile()` at startup, and the auto-recovery policy.
- **Outputs:** `RecoveryAttempt` records, plus a restored instance handed to `supervisor.adopt_restored`.
- **Depends on:** `storage` (`verify`, the checkpoint record), `backend` (`restore`, `status`), `supervisor`, `compat`, and `events`.
- **Failure modes:**
  - A precheck fails: `CORRUPT`, `UNTRUSTED_CHECKPOINT`, `ARCH_MISMATCH`, `MACHINE_MISMATCH`, `VERSION_MISMATCH`, or `CONFLICT`.
  - The backend restore fails (`RESTORE_FAILED`).
  - Verification fails (`VERIFY_FAILED`).
  - The daemon crashes partway (`INTERRUPTED`).
  - In every case the checkpoint is kept (FR-027).
- **Security boundary:** restores only catalog-trusted checkpoints owned by the user (SEC-007). There is no import path.

### Event logger (`events`)

- **Responsibility:**
  - Append `SystemEvent` rows.
  - Emit structured JSON logs to stdout, which reach journald.
  - Fan events out to IPC subscribers (FR-033, NFR-003, SEC-009).
- **Inputs:** `emit(type, source, severity, data, workload_id?, operation_id?, simulated=False)`.
- **Outputs:** event rows with a monotonically increasing sequence number, plus subscriber messages.
- **Depends on:** `storage` (the event table).
- **Failure modes:**
  - The catalog is unavailable: events are buffered in memory (bounded at 1,000) and written to the log stream. Buffer overflow is itself logged.
  - A slow subscriber: its per-subscriber queue is bounded, and the subscriber is dropped with `SUBSCRIBER_OVERFLOW`.
- **Security boundary:** never logs environment values or full argument arrays. It logs the executable path and a SHA-256 of the argument list instead.

### IPC server (`ipc`)

- **Responsibility:** the local socket, framing, authentication, schema validation, method dispatch, and event subscriptions ([IPC and security](ipc-security.md)).
- **Inputs:** framed requests. **Outputs:** framed responses and events.
- **Depends on:** `daemon` (method table) and every component it dispatches to.
- **Failure modes:**
  - Malformed or oversized frames: the error is returned and the connection closed.
  - Authentication failure: the connection is rejected and audited.
  - Too many concurrent requests per connection: `BUSY`.
- **Security boundary:** this is the trust boundary between client and daemon (SEC-001, SEC-002, SEC-005).

### Daemon service core (`daemon`)

- **Responsibility:**
  - Run the startup and shutdown state machine ([data model](data-model.md#daemon-startup-and-shutdown)).
  - Hold a single-instance lock (`flock` on `$XDG_RUNTIME_DIR/napguard/daemon.lock`).
  - Load configuration, wire the components together, and integrate with systemd (`Type=notify` readiness, if adopted in Stage 06).
- **Failure modes:**
  - Another instance is running: exit code 3 with `ALREADY_RUNNING`.
  - The catalog is corrupt: exit code 4. Repeated restarts are bounded by the unit's `StartLimitBurst`.
  - Insecure runtime paths: exit code 5.

### CLI (`cli`) and GUI (`gui`)

- **Responsibility:** clients of the IPC API. The GUI is PySide6. The CLI is a scriptable `napguard` command.
- **Contract:** they use only the IPC methods and events (NFR-005). They never touch the catalog, artifacts, or processes directly.
- **Failure modes:** the daemon is unreachable: the client shows `DAEMON_UNAVAILABLE` with an action ("start with `systemctl --user start napguard`").

## Persistent record ownership

Only the owning component writes a record. Others read through the owner's API or through read-only catalog queries.

| Record | Owner | Writers through the owner's API |
|---|---|---|
| `Workload` | `registry` | `compat` (compatibility fields only) |
| `ProtectionPolicy` | `registry` | — |
| `ProcessInstance` | `supervisor` | `recovery` (via `adopt_restored`) |
| `Checkpoint` (row, manifest, artifacts) | `storage` | `ckpt` (via `publish`), `recovery` (sets `restore_verified_at` via storage) |
| `CheckpointOperation` | `ckpt` | — |
| `RecoveryAttempt` | `recovery` | — |
| `SystemEvent` | `events` | everyone, via `emit` |
| Schema version and migrations | `storage` | — |

## Process containment (design constraint, decision open)

Child processes inherit the daemon's systemd cgroup. With the default `KillMode=control-group`, stopping or restarting `napguard.service` would kill every managed workload, which violates FR-030. Possible approaches:

1. Launch each workload, together with its backend helper processes such as a DMTCP coordinator, in its own transient user scope (`systemd-run --user --scope` or `StartTransientUnit` over D-Bus).
2. Set `KillMode=process` on the daemon unit.

Option 1 is preferred, because each workload then gets its own cgroup for accounting and clean stopping. The decision is recorded as OD-03 in the [Stage 01 report](../phases/STAGE-01-report.md). Stage 02 experiments must record which processes each backend creates, so the containment design covers all of them.
