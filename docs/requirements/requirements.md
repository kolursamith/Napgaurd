# NapGuard Requirements Specification

- Status: Stage 01 draft for Linux review
- Date: 2026-10-19
- Baseline: commit `931369dab8c84534160821039b863f38c7f737b3` (Stage 00)
- Related: [architecture overview](../architecture/overview.md), [traceability matrix](traceability.md)

NapGuard is a Linux desktop application with a user-level background service. It selectively checkpoints compatible, user-registered processes through a verified checkpoint backend, stores the checkpoints durably, and restores those processes after termination or reboot. This document is the single source of truth for requirements. The design documents reference these IDs and do not restate requirements.

## Conventions

- Each requirement ID has the form `<CATEGORY>-<NNN>`, and IDs are never reused. If a requirement is withdrawn, its row stays with the text "Withdrawn".
- "MUST" is mandatory for the stage that owns the requirement (see the [traceability matrix](traceability.md)).
- **Verification codes:**
  - U: unit test
  - I: integration test
  - S: Linux system test on Ubuntu
  - F: failure / fault-injection test
  - M: measurement with recorded raw data
  - R: review / inspection
  - D: live demonstration
- A requirement is satisfied only when its acceptance criterion is shown with the listed methods on the verified platform. Mac-only results never satisfy S, M, or D.
- "Backend" means the checkpoint engine chosen in Stage 02 (DMTCP or CRIU). Backend behaviour stays unverified until Stage 02 evidence exists ([backend contract](../architecture/checkpoint-backend.md)).

## 1. Functional requirements

| ID | Requirement | Acceptance criterion | Verification |
|---|---|---|---|
| FR-001 | NapGuard MUST let the user register a workload with an absolute executable path, an argument array, a working directory, an environment policy, and a backend preference. It MUST assign a stable workload ID that is independent of any PID. | Registration returns a UUID. The record is still identical after a daemon restart and a reboot. No catalog table uses a PID as a key. | U, I, S |
| FR-002 | Registration MUST validate its inputs and reject a workload when: the executable is relative, missing, not a regular file, not executable by the user, or group- or world-writable; the working directory is not an accessible directory; or any argument contains NUL. | Each listed invalid case returns `INVALID_ARGUMENT` with a field-specific detail and creates no record. | U, F, S |
| FR-003 | The user MUST be able to update, disable, and remove a workload. Removal MUST be refused while the workload has a live process or active operation. Removal MUST keep its checkpoints unless the user explicitly requests a purge. | Update increments `config_version`. Removing an active workload returns `CONFLICT`. Removal without purge leaves the published checkpoint directories and rows in place, marked `ORPHANED_WORKLOAD`. | U, I |
| FR-004 | NapGuard MUST list workloads with their lifecycle state, compatibility status, protection policy, and latest valid checkpoint. | `workload.list` returns every non-removed workload with these four fields filled in, or explicitly null. | U, I |
| FR-005 | Before protection is enabled, the compatibility manager MUST assess each workload with a real backend probe. The result is one of `UNSUPPORTED`, `UNKNOWN`, `CANDIDATE`, or `VERIFIED`, with machine-readable reason codes. | No code path produces `CANDIDATE` or `VERIFIED` without a successful probe. Every `UNSUPPORTED` or `UNKNOWN` result carries one or more reason codes. | U, I, S |
| FR-006 | A workload's compatibility becomes `VERIFIED` only after a real checkpoint and restore of that workload succeed on the same backend, backend version, and CPU architecture. | Status changes to `VERIFIED` only in a transaction that references a `SUCCEEDED` recovery attempt. Changing the backend version reverts the status to `CANDIDATE`. | U, I, S |
| FR-007 | NapGuard MUST launch registered workloads under backend management and record a process instance. The record holds the PID, the kernel start time, the boot ID, and backend session data. | Every launch writes one active `ProcessInstance` row whose PID and `/proc/<pid>/stat` start time match the running process. | I, S |
| FR-008 | NapGuard MUST detect when a managed workload exits and classify it as completed, stopped by the user, crashed, or lost. | In system tests, the state and exit code or signal are recorded within 2 s of the exit for all four classes. | I, S, F |
| FR-009 | Stopping a workload MUST send SIGTERM to its managed process group only. If the group is still alive after a configurable grace period (default 10 s), NapGuard sends SIGKILL. Signals use a pidfd. | Stopping a workload that ignores SIGTERM ends it within grace + 2 s. A concurrently running unrelated process owned by the same user survives. | I, S, F |
| FR-010 | A manual "checkpoint now" request MUST produce a real backend checkpoint. It immediately returns an operation ID; completion is reported asynchronously. | The request is acknowledged within 200 ms with an operation ID. The operation ends `SUCCEEDED` with a published checkpoint, or ends with an error code. | I, S |
| FR-011 | By default, a checkpoint MUST leave the workload running after capture. | After a successful checkpoint of a counting workload, the counter keeps increasing for at least 10 s. | S |
| FR-012 | NapGuard MUST list each workload's checkpoint history with ID, generation, creation time, trigger, size, backend and version, state, integrity result, and restore-verified time. | `checkpoint.list` returns all of these fields for every non-deleted checkpoint. | U, I |
| FR-013 | NapGuard MUST checkpoint each protected workload periodically at its policy interval, with a minimum of 60 s. | Over a 30 min run with a 5 min interval, the gap between consecutive checkpoint starts is at least the interval and at most interval + the previous operation's duration + 10 s. | U, I, S, M |
| FR-014 | At most one checkpoint operation per workload may be queued or running. Additional requests MUST be merged into the pending one, which keeps the higher priority. | Ten concurrent requests for one workload create exactly one operation. Its trigger is the highest-priority trigger among the requests. | U, I |
| FR-015 | The number of simultaneous checkpoint operations across all workloads MUST NOT exceed a configurable bound (default 1). | With three workloads due at once, at most `max_concurrent` operations are ever `RUNNING`, as checked from the operation timestamps. | U, I, S |
| FR-016 | Queued operations MUST start in priority order: pre-suspend > critical battery > low battery > manual > periodic. Ties are ordered by enqueue time. | A test that enqueues all trigger types records start times in exactly this order. | U |
| FR-017 | While Linux is awake, NapGuard MUST read battery state from `/sys/class/power_supply`. It MUST handle zero, one, or several batteries and an optional AC adapter. | Parser tests on captured sysfs trees with 0, 1, and 2 batteries give the specified aggregate capacity and charging state. A system test records the real sysfs contents of the VM. | U, S |
| FR-018 | Low-battery and critical-battery triggers MUST use the configured thresholds with hysteresis. Each trigger fires at most once per discharge episode. | A simulated discharge 30%→5% with threshold 20%, hysteresis 5%, and critical 10% triggers exactly one low and one critical request. The trigger re-arms only above 25% or when AC comes online. | U, I |
| FR-019 | If no battery is exposed, battery triggers MUST be inactive and reported as such, and periodic and pre-suspend checkpoints MUST keep working. | With an empty power_supply tree, `daemon.capabilities` reports `battery: absent`, and the periodic schedule still produces checkpoints. | U, S |
| FR-020 | On a logind `PrepareForSleep(true)` signal, NapGuard MUST run its pre-suspend checkpoint policy under a held delay inhibitor. It MUST release the inhibitor before the budget runs out. | In a system test, the inhibitor shows in `systemd-inhibit --list` while awake. In the event log, the time from the signal to inhibitor release is at most the budget computed from `InhibitDelayMaxUSec`. | I, S, M |
| FR-021 | After a `PrepareForSleep(false)` signal (resume), NapGuard MUST take a new delay inhibitor and reconcile in-flight operations. | After a resume, either real or on a labeled simulated bus, a new inhibitor is listed within 2 s and no operation stays `RUNNING` past its deadline. | I, S |
| FR-022 | At startup, the daemon MUST reconcile the catalog against the on-disk artifacts and live processes, then list recovery candidates. | After a cold boot with one published checkpoint, `recovery.candidates` lists it and marks the previous instance `LOST`. | I, S |
| FR-023 | By default, restoring MUST require explicit user confirmation of a specific checkpoint. | With default policy, no restore runs without a `recovery.restore` request that names a checkpoint ID and has `confirm: true`. | U, I |
| FR-024 | Automatic recovery MUST be off by default. It runs only when enabled per workload, and at most once per checkpoint per boot. | With auto-recovery enabled, a reboot produces one attempt. A second daemon restart in the same boot produces none. | U, I, S |
| FR-025 | NapGuard MUST refuse to restore a workload that has a live instance or another active recovery attempt. | Concurrent restore requests produce one attempt; the others get `CONFLICT`. Restoring while the original process is alive gets `CONFLICT`. | U, I, F |
| FR-026 | After restoring, NapGuard MUST confirm that the process is alive, owned by the user, and recorded with origin `RESTORE`. Success is reported only if the process stays alive for the verification window (default 10 s). | `RecoveryAttempt.state = SUCCEEDED` only after the window. A process that dies inside the window results in `FAILED` with code `VERIFY_FAILED`. | I, S, F |
| FR-027 | A failed restore MUST keep the checkpoint and record the error. | After an injected restore failure, the checkpoint is still `PUBLISHED` and an attempt with an error code exists. | I, F |
| FR-028 | NapGuard MUST offer a plain restart from the beginning that is separate from checkpoint recovery and labeled as a restart everywhere. | Restart creates an instance with origin `LAUNCH`, never `RESTORE`. The GUI and CLI label it "Restart (progress not restored)". | U, I, R |
| FR-029 | The daemon MUST run as a systemd user service and keep managing workloads after the GUI or CLI closes. | With the GUI closed, `systemctl --user is-active napguard` reports active, and a scheduled checkpoint still completes. | S |
| FR-030 | Restarting the daemon MUST NOT kill managed workloads or corrupt the catalog. | After `systemctl --user restart napguard`, managed workloads are still alive with the same PID and start time. `PRAGMA integrity_check` returns `ok`. | S, F |
| FR-031 | The GUI MUST support registering workloads, viewing compatibility, starting and stopping, requesting a checkpoint, editing policy, viewing checkpoint history and the event log, and restoring. | A scripted demonstration completes each workflow against the real daemon. | I, D |
| FR-032 | A command-line client MUST provide the same operations through the IPC API, so it can be scripted and tested. | Every IPC method has a CLI command. The CLI exits nonzero with the error code when a request fails. | U, I, S |
| FR-033 | NapGuard MUST record every significant operation as an event with outcome, duration, error code, and related IDs. | Every terminal state of every operation and recovery attempt has a matching `SystemEvent`. | U, I |
| FR-034 | Unsupported or unknown workloads MUST be reported with reason codes and a human-readable explanation. | Every reason code has a message entry, and the GUI and CLI show both. | U, R |

## 2. Nonfunctional requirements

| ID | Requirement | Acceptance criterion | Verification |
|---|---|---|---|
| NFR-001 | Code MUST be typed Python 3.12 that passes mypy (strict) and Ruff. | The Ubuntu stage run of `mypy src tests` and `ruff check .` exits 0. | S, R |
| NFR-002 | Runtime dependencies MUST be minimal. Every new runtime dependency needs an ADR. | `pyproject.toml` runtime dependencies equal the ADR-recorded list. | R |
| NFR-003 | Logs MUST be structured JSON lines that carry operation and workload IDs. | Every daemon log line parses as JSON with `ts`, `level`, and `event` keys. | U, I |
| NFR-004 | All OS integrations (backend, sysfs, D-Bus, systemd, process control) MUST sit behind typed interfaces so policy logic can be unit-tested without Linux facilities. | Unit tests pass on macOS and Ubuntu with no Linux facility access. | U, R |
| NFR-005 | The GUI MUST depend only on the IPC contract and never import daemon, storage, or backend modules. | An automated import check fails if `napguard.ui` imports any of these. | U |
| NFR-006 | Every stage MUST end with a report in `docs/phases/` that includes a gate table, the commands run, and evidence. | Every report from Stage 00 onward exists and contains a gate table. | R |
| NFR-007 | Daemon idle footprint, as initial targets that may be revised only by ADR: under 1% average CPU and under 150 MB RSS over 10 min idle with two managed workloads. | A measurement report records the raw samples. | M |
| NFR-008 | Stored timestamps MUST be UTC ISO 8601. Durations MUST come from a monotonic clock. | Schema review and unit tests confirm both. | U, R |

## 3. Linux platform requirements

| ID | Requirement | Acceptance criterion | Verification |
|---|---|---|---|
| PLT-001 | The primary verified platform is Ubuntu 24.04 LTS on aarch64 (UTM VM). Every support statement MUST name its OS and architecture. | Each stage's Ubuntu evidence records `/etc/os-release` and `uname -m`. No document claims support without naming the platform. | S, R |
| PLT-002 | NapGuard MUST run on the Ubuntu system Python 3.12. | The Stage 00 baseline script passes on Ubuntu 24.04 with Python 3.12.3. | S |
| PLT-003 | The daemon requires a systemd user manager and MUST fail with a clear error if none is available. | Starting without a user manager exits nonzero with `PLATFORM_UNSUPPORTED` and a message. | S, F |
| PLT-004 | Sleep integration requires the system D-Bus and systemd-logind. If either is missing, the sleep features MUST be disabled and reported, not crash the daemon. | With logind unreachable (simulated bus), `daemon.capabilities` reports `sleep: unavailable` and the daemon keeps serving. | I, F |
| PLT-005 | Power-supply access MUST be read-only and must not require root. | Code review finds no writes under `/sys`. The system test runs as an ordinary user. | R, S |
| PLT-006 | Backend tools MUST be found at runtime by capability probing. Their absolute paths and versions MUST be recorded. | `daemon.capabilities` lists each backend with `available`, `path`, `version`, and `verified` fields. | U, S |
| PLT-007 | The data directory MUST be on persistent storage. NapGuard MUST refuse tmpfs and ramfs. | When the data directory is on tmpfs, startup fails with `STORAGE_NOT_PERSISTENT`. | U, S, F |
| PLT-008 | Other architectures and distributions stay unverified until tested, and MUST be documented that way. | The final support matrix lists every untested platform as "unverified". | R |
| PLT-009 | The kernel facilities the chosen backend needs MUST be checked at startup. | The probe runs the backend's own self-check (for example `criu check`) or an equivalent, and records the result. | S |

## 4. Security requirements

| ID | Requirement | Acceptance criterion | Verification |
|---|---|---|---|
| SEC-001 | The IPC socket MUST be `$XDG_RUNTIME_DIR/napguard/daemon.sock`, with the directory mode 0700 and the socket mode 0600, both owned by the user. The daemon MUST refuse to start if those paths have wrong ownership or modes. | `stat` shows 0700/0600 and the correct owner. A pre-created group-writable directory makes startup fail with `INSECURE_PATH`. | U, S, F |
| SEC-002 | Every connection MUST be authenticated with `SO_PEERCRED`. Connections whose UID differs from the daemon's UID MUST be rejected and audited. | A connection from another local user (or from root) gets `UNAUTHENTICATED`, is closed, and produces an `AUTH_REJECTED` event. | I, S, F |
| SEC-003 | NapGuard MUST signal, checkpoint, or restore only processes it launched or restored. Each process is identified by PID, start time, and boot ID, and pidfds are used where available. | Requests that name an arbitrary PID do not exist in the API. After a PID-reuse simulation, a signal targets nothing and the result is `PROCESS_GONE`. | U, I, F |
| SEC-004 | Subprocesses MUST be started with argument arrays and never through a shell. They get a closed fd set, an explicit environment, and `start_new_session`. | Code review plus a test find no `shell=True` or `os.system`. An argument containing `;rm -rf` is passed literally. | U, R, F |
| SEC-005 | IPC input MUST be validated against per-method schemas: unknown fields rejected, strings length-limited, messages at most 1 MiB. | Fuzz and malformed-frame tests produce `INVALID_ARGUMENT` or `FRAME_TOO_LARGE` and never crash the daemon. | U, F |
| SEC-006 | Checkpoint directories MUST be mode 0700 and their files 0600, owned by the user. Artifacts with the wrong owner or mode MUST NOT be restored. | After `chmod 644` on an artifact, the restore precheck fails with `INSECURE_ARTIFACT`. | U, S, F |
| SEC-007 | NapGuard MUST restore only checkpoints that it created and recorded in its catalog, whose manifest digest matches the catalog. Importing external checkpoints is not supported. | A changed manifest or a copied-in foreign directory is never offered as a recovery point. Attempting it returns `UNTRUSTED_CHECKPOINT`. | U, I, F |
| SEC-008 | Workloads MUST get an allow-listed environment by default, not the daemon's full environment. The user MUST be warned that checkpoint images contain process memory, which may include secrets. | The default policy passes only `PATH`, `HOME`, `LANG`, `LC_*`, `TERM`, and the variables configured for the workload. The warning appears at registration. | U, R |
| SEC-009 | Security-relevant actions MUST be audit-logged: authentication rejections, workload register/remove, signals sent, checkpoint deletion, restore requests and results, policy changes, and integrity failures. | Each action creates a `SystemEvent` with `category = security`. | U, I |
| SEC-010 | The daemon MUST run without root and without added capabilities. Any backend need for privileges or file capabilities requires an ADR and explicit user approval first. | The process status shows a non-zero UID and empty `CapEff`. No setuid or file capability is introduced without an ADR. | S, R |
| SEC-011 | Artifact handling MUST NOT follow symlinks. Manifest paths MUST be relative, normalized, and inside the checkpoint directory. | Manifests containing `../x`, absolute paths, or symlink artifacts are rejected with `INVALID_MANIFEST`. | U, F |

## 5. Reliability and crash-consistency requirements

| ID | Requirement | Acceptance criterion | Verification |
|---|---|---|---|
| REL-001 | New checkpoints MUST be written to a staging directory and published by atomic rename. Incomplete checkpoints MUST NOT be advertised. | No API returns a checkpoint whose state is not `PUBLISHED`. Fault injection at every publication step never yields an advertised incomplete checkpoint. | U, I, F |
| REL-002 | Artifacts, manifest, and directories MUST be fsynced in the documented order before the catalog commit that sets `PUBLISHED`. | Tracing of the publication sequence shows fsync calls in the [specified order](../architecture/storage-recovery.md#atomic-publication-protocol). | U, I |
| REL-003 | The catalog MUST be SQLite in WAL mode with `synchronous=FULL` and foreign keys on. Every state change MUST be one transaction. | At startup, the pragmas read back as specified. A crash test leaves `PRAGMA integrity_check` returning `ok`. | U, I, F |
| REL-004 | The previous valid checkpoint MUST stay in place until its replacement is published. | Killing the daemon during a new checkpoint always leaves the previous checkpoint `PUBLISHED` and restorable. | I, F, S |
| REL-005 | At startup, NapGuard MUST reconcile: abandoned staging directories are removed, catalog rows with missing artifacts become `MISSING`, and unknown directories are quarantined without being advertised. | Each injected inconsistency produces the specified outcome and an event. | U, I, F |
| REL-006 | Artifact SHA-256 hashes MUST be checked before every restore. A mismatch makes the checkpoint `CORRUPT` and not restorable. | Flipping a single byte in any artifact leads to `CORRUPT` and a refused restore, while an older valid checkpoint is offered instead. | U, I, F |
| REL-007 | A hard VM power-off during a checkpoint MUST leave, after boot, the previous checkpoint advertised and restorable, and no partial checkpoint advertised. | A recorded cold-boot test on Ubuntu meets this in at least 3 trials. | S, F |
| REL-008 | Every backend call MUST have a timeout. A timed-out operation MUST be `TIMED_OUT`, its staging directory cleaned, and the workload left running if it was running. | An injected backend hang produces `TIMED_OUT` within the deadline + 5 s. | U, I, F |
| REL-009 | Operations still active when the daemon crashes MUST be marked `INTERRUPTED` at the next startup and never left `RUNNING`. | After a `kill -9` of the daemon during a checkpoint, startup marks the operation `INTERRUPTED` and cleans up its staging. | I, F |
| REL-010 | NapGuard MUST enforce a global quota and per-workload quotas, and check free space before starting. It MUST NOT delete the last valid checkpoint to make space. | With free space below the estimate, a checkpoint fails with `INSUFFICIENT_SPACE` and the previous checkpoints remain. An ENOSPC injected mid-write produces the same result. | U, I, F |
| REL-011 | Retention MUST keep the newest N published checkpoints per workload (default 3). Older ones are deleted only after a newer one is published, and the newest restore-verified checkpoint is never deleted. | Retention tests over generation sequences match the specification exactly. | U, I |

## 6. Performance measurement requirements

These requirements set no performance targets, because checkpoint cost is unknown until it is measured. They require that the costs are measured and reported honestly.

| ID | Requirement | Acceptance criterion | Verification |
|---|---|---|---|
| PRF-001 | Checkpoint duration, from request to `PUBLISHED`, broken into capture and validation, MUST be recorded for every checkpoint. | Each operation row has `duration_ms`. Each manifest records the capture and validation durations. | U, M |
| PRF-002 | Checkpoint image size (total and per artifact) MUST be recorded. | Every manifest and catalog row records the byte counts. | U, M |
| PRF-003 | Restore duration, from request to verified, MUST be recorded. | Every `RecoveryAttempt` records `duration_ms`. | U, M |
| PRF-004 | Progress lost between the last checkpoint and the interruption MUST be measured using test workloads that report observable progress. That progress is a verification signal only, never a checkpoint mechanism. | The Stage 02 and Stage 09 reports give the lost-progress figure for each trial. | M, S |
| PRF-005 | The slowdown that checkpointing causes in workloads MUST be measured. | The Stage 12 report compares workload runtime with and without periodic checkpointing (at least 5 runs each). | M |
| PRF-006 | Pre-suspend checkpoint latency MUST be recorded against the inhibitor budget. | Each pre-suspend event records its budget, its elapsed time, and which workloads were skipped. | M, I |
| PRF-007 | Benchmarks MUST use at least 5 runs, report mean, minimum, and maximum, record the environment, and keep the raw data. | Benchmark reports include raw CSV/JSON in the repository or the report. | R, M |

## 7. Compatibility requirements

| ID | Requirement | Acceptance criterion | Verification |
|---|---|---|---|
| CMP-001 | Stage 02 MUST produce a compatibility matrix by actually checkpointing and restoring representative unmodified workloads on Ubuntu 24.04 aarch64. The workload classes: single-threaded C, multi-threaded C, a Python computation, a process writing a regular file, a parent with a child process, a process holding a TCP connection, and a GUI application. | For each class, the matrix records checkpoint and restore outcomes, commands, versions, and evidence for each backend tried. | S, M, R |
| CMP-002 | A workload class MAY be described as supported only for a backend, version, and architecture with recorded passing evidence. | Review finds no support claim without a matrix entry. | R |
| CMP-003 | Workloads that match no verified class MUST be reported `UNKNOWN`. Protection may be enabled for them only after an explicit warning. | Enabling protection on an `UNKNOWN` workload requires `acknowledge_unverified: true`. | U, I |
| CMP-004 | The backend name and version MUST be recorded in every manifest. Restore MUST refuse an incompatible backend version. | Restoring after changing the backend version returns `VERSION_MISMATCH` unless an ADR has declared that pairing compatible. | U, F |
| CMP-005 | The CPU architecture, kernel release, and boot ID MUST be recorded per checkpoint. Restore MUST refuse an architecture mismatch. | A manifest with a different architecture returns `ARCH_MISMATCH`. | U, F |
| CMP-006 | Known-unsupported conditions found in Stage 02 (for example static binaries or setuid executables for the chosen backend) MUST become explicit compatibility rules. | Each Stage 02 negative finding maps to a reason code covered by a test. | U, R |

## 8. Recovery limitations

These requirements state what NapGuard must disclose and must not promise.

| ID | Requirement | Acceptance criterion | Verification |
|---|---|---|---|
| LIM-001 | Work done after the last checkpoint is lost. NapGuard MUST show the age of the newest checkpoint. | The GUI and CLI show the time since the newest published checkpoint for each workload. | U, R |
| LIM-002 | External side effects (file writes, database changes, messages sent) are not rolled back. The restore confirmation MUST say so. | The restore dialog and the CLI prompt contain the side-effect warning. | R, D |
| LIM-003 | Network connections and remote sessions are not guaranteed to be restored. NapGuard MUST report the behaviour measured in Stage 02. | The TCP class result in the matrix is shown as a limitation on matching workloads. | S, R |
| LIM-004 | GPU, device, and hardware state is not restored, and such workloads MUST be marked unsupported. | Workloads that open `/dev/dri` or similar device nodes at assessment time get the reason `DEVICE_STATE`. | U, R |
| LIM-005 | NapGuard does nothing while the system is genuinely suspended. The documentation and UI MUST NOT suggest otherwise. | Review finds no claim of monitoring during suspend. | R |
| LIM-006 | A restored process may have a different PID. Identity is always the workload ID plus the instance ID. | After a restore, the instance record gives the new PID, and the workload ID is unchanged. | I, S |
| LIM-007 | Files that changed after the checkpoint may make a restore fail or behave inconsistently. NapGuard MUST report restore errors caused by changed files. | Modifying a file the workload had open before restoring yields either a backend error surfaced as `RESTORE_FAILED`, or a successful restore recorded with a warning. Stage 02 observes which outcome occurs. | S, F |

## 9. User interaction requirements

| ID | Requirement | Acceptance criterion | Verification |
|---|---|---|---|
| UX-001 | Every long-running operation MUST show pending, running, succeeded, or failed, with the reason. | The GUI views update from IPC events within 1 s of each state change. | I, D |
| UX-002 | A restore MUST be confirmed in a dialog showing the checkpoint time, its age, the backend, and the limitations. | The dialog has these fields, and canceling it sends no request. | I, R |
| UX-003 | A workload MUST NOT be shown as "Protected" unless its policy is enabled, it is not `UNSUPPORTED`, and it has at least one published checkpoint. | A unit test of the view-model status function covers every combination. | U |
| UX-004 | Closing the GUI MUST NOT stop workloads, and the GUI MUST say so the first time it closes. | Closing the window leaves the daemon and workloads running, and the notice appears once. | S, D |
| UX-005 | Simulated or replayed power and sleep events MUST be labeled "simulated" in events, logs, and the UI. | Every event from a simulated provider has `simulated = true`, and the UI shows a badge for it. | U, I |
| UX-006 | Every error shown to the user MUST include a stable error code and a suggested action. | Every IPC error code has a message and an action entry. | U, R |

## 10. Installation and packaging requirements

| ID | Requirement | Acceptance criterion | Verification |
|---|---|---|---|
| PKG-001 | NapGuard MUST install on a clean Ubuntu 24.04 aarch64 system by a documented, reproducible method that needs no root for NapGuard itself. | Following the install guide on a fresh VM snapshot produces a working installation. The transcript is recorded. | S, D |
| PKG-002 | The systemd user unit MUST be installed and enabled per user, without root. | `systemctl --user enable --now napguard` succeeds after installation. | S |
| PKG-003 | A desktop entry MUST start the GUI. | The launcher appears in the Ubuntu application grid and opens the GUI. | S, D |
| PKG-004 | Uninstalling MUST remove program files, the unit, and the desktop entry, and keep user data unless a purge is requested. | After uninstalling, no NapGuard files remain outside the XDG data and config directories. The purge option removes those too. | S |
| PKG-005 | Missing backend dependencies MUST be reported at startup with installation guidance. NapGuard MUST NOT install system packages itself. | Without the backend installed, the daemon starts with `backend: unavailable` and shows the guidance text. | S, F |
| PKG-006 | Data, config, and runtime files MUST use the XDG directories: `$XDG_DATA_HOME/napguard`, `$XDG_CONFIG_HOME/napguard`, and `$XDG_RUNTIME_DIR/napguard`. | Resolving the paths with and without the XDG variables set gives the specified locations. | U |

## 11. Explicitly excluded capabilities

| ID | Requirement | Acceptance criterion | Verification |
|---|---|---|---|
| EXC-001 | NapGuard does not support arbitrary Linux applications. Support is limited to verified workload classes. | No document or UI text claims universal support. | R |
| EXC-002 | NapGuard does not manage root-owned processes or other users' processes. | The API cannot target processes NapGuard did not launch (see SEC-003). Registering a setuid executable is rejected. | R, F |
| EXC-003 | Checkpointing GPU or device state is excluded. | Covered by LIM-004's reason code. | R |
| EXC-004 | Preserving live network sessions is excluded, beyond whatever Stage 02 measures and reports. | Covered by LIM-003. No claim that connections are preserved. | R |
| EXC-005 | NapGuard does not implement hibernation and does not monitor during suspend. | Review finds no hibernation code and no claim of monitoring during suspend. | R |
| EXC-006 | Application-written progress files, or native save/load adapters, are not accepted as a substitute for process checkpointing. | Every published checkpoint comes from a backend process image. Review finds no progress-file recovery path. | R |
| EXC-007 | Containers, multi-user management, and remote or cloud checkpoint storage are excluded. | Review finds no such code paths. | R |
| EXC-008 | Attaching to processes that NapGuard did not launch is excluded from the first release. | The backend contract has no attach operation in v1. | R |
| EXC-009 | Validation on physical battery and lid hardware is not claimed from VM testing. | Reports label every VM battery result "simulated" or "VM", never "hardware". | R |
| EXC-010 | Migrating checkpoints between machines or architectures is excluded. | The manifest records a SHA-256 of `/etc/machine-id`. Restoring on a different machine returns `MACHINE_MISMATCH`, and an architecture mismatch is refused (CMP-005). A changed boot ID on the same machine is allowed. | U, F |
