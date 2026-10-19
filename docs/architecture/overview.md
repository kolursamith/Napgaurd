# NapGuard Architecture Overview

## Scope and status

This document describes the proposed component boundaries for NapGuard. Layer 0 creates only package boundaries and development tooling. It does not prove checkpoint compatibility or implement daemon, power, sleep, recovery, or user-interface behavior.

NapGuard targets Ubuntu 24.04. macOS may be used for portable editing and unit checks, but Linux integration claims require evidence from the verified target environment.

The first release targets Ubuntu on ARM64 (aarch64), the architecture of the UTM development VM. Support on x86-64 Linux must be demonstrated separately.

## Target system architecture

This is a logical architecture: the boxes are responsibilities, not necessarily separate OS processes. The first release is planned as one user-level daemon containing the managers below, plus a separate GUI process.

```text
GUI (PySide6)
  |
  |  authenticated Unix-domain socket
  |
User-level NapGuard daemon
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
  +-- Real checkpoint backend: DMTCP or CRIU, subject to feasibility
  |
  +-- SQLite metadata and persistent checkpoint images
  |
Ubuntu Linux: processes, filesystems, systemd, D-Bus, power interfaces
```

Only the package boundaries listed under "Component model" exist today, and they are empty. The workload registry, process supervisor, compatibility manager, scheduler, and event logger have no packages yet; they will be added in the stage that implements them rather than as empty placeholders.

## Detailed design (Stage 01)

This overview stays the high-level summary. The detailed Stage 01 design lives in these documents:

- [Requirements specification](../requirements/requirements.md) and [stage traceability matrix](../requirements/traceability.md)
- [Component architecture and contracts](components.md)
- [Data and state models](data-model.md)
- [Checkpoint backend contract](checkpoint-backend.md)
- [Storage and recovery design](storage-recovery.md)
- [Power and sleep design](power-sleep.md)
- [IPC and security design](ipc-security.md)
- Decisions: [ADR 0001](../decisions/0001-technology-stack.md), [ADR 0002](../decisions/0002-ipc-protocol-and-authentication.md), [ADR 0003](../decisions/0003-checkpoint-publication-and-catalog-durability.md)

## Architectural invariants

1. NapGuard is Linux-only. macOS is only a development host.
2. GitHub is source control, not an execution environment. Ubuntu is the authoritative environment for Linux tests.
3. Workload configurations are keyed by stable workload IDs, never solely by PIDs, because PIDs change across restore.
4. Checkpoint images and metadata are stored separately: images on a persistent filesystem, metadata in SQLite.
5. Only validated, complete checkpoints are advertised as recovery points.
6. The GUI never manipulates Linux process state directly; it sends requests to the daemon.
7. The daemon operates on user-owned workloads by default.
8. Checkpoint support is capability-dependent and is never promised universally; unsupported workloads are reported as such.
9. Power monitoring happens only while Linux is awake.
10. Pre-suspend work respects systemd-logind timing and inhibitor semantics (bounded delay inhibitors).
11. Checkpoint recovery (restoring saved execution state) is distinct from ordinary application restart and is reported separately.
12. Restoring external side effects, remote connections, and device state is not guaranteed.

## Development and verification workflow

1. Source is developed on macOS.
2. Changes are reviewed and committed to Git.
3. Commits are pushed to GitHub only after user authorization.
4. Ubuntu in UTM pulls the exact commit (no SSH is required).
5. Linux tests run inside Ubuntu, and the Ubuntu-side `git rev-parse HEAD` is recorded against the Mac commit.
6. Failures are fixed on macOS and the cycle repeats.
7. A stage passes only after all mandatory Linux checks pass, and the next stage starts only on explicit approval.

## Component model

### Desktop UI (`napguard.ui`)

The future PySide6 application will let a user register supported workloads, inspect protection status, request checkpoints, and review recovery results. It will be an unprivileged client of the daemon and will not own checkpoint lifecycle state.

### Daemon (`napguard.daemon`)

An asyncio-based user service will own job supervision, scheduling, authenticated local IPC, and orchestration. A user-level systemd unit is preferred to avoid unnecessary root privileges.

### Core (`napguard.core`)

Core will hold typed domain models, configuration rules, compatibility status, scheduling policies, and interfaces shared by other components. It must not depend on PySide6 or Linux hardware APIs.

### Checkpoint engines (`napguard.checkpoint`)

Checkpoint support will use a pluggable interface. DMTCP is the first candidate for compatible unmodified applications. CRIU is optional and depends on kernel features and permissions. Per-application adapters may handle workloads that cannot be restored transparently. Backend support remains unproven until Layer 1 performs real checkpoint/restart tests.

### Storage (`napguard.storage`)

SQLite will store metadata. Checkpoint images will live on a persistent Linux filesystem. Generation commits must preserve the previous valid checkpoint until the new generation has been flushed, synced, atomically renamed, and its containing directory synced where applicable.

### Power (`napguard.power`)

Future providers will inspect Linux `power_supply` sysfs while the system is awake. Replay or injected providers used by tests must be explicitly labeled. The design must not claim polling continues during genuine suspend.

### Sleep (`napguard.sleep`)

Future integration will observe systemd-logind over D-Bus and may use bounded delay inhibitors for pre-suspend work. macOS sleep APIs are outside the product target.

### Recovery (`napguard.recovery`)

Recovery will validate durable metadata and checkpoint integrity before attempting restoration after cold boot. Restore failure must be isolated per job and surfaced without deleting the last valid checkpoint.

## Dependency direction

- UI depends on IPC contracts, not daemon internals.
- Daemon coordinates core, checkpoint, storage, power, sleep, and recovery interfaces.
- OS integrations sit behind typed interfaces so unit tests can remain portable and system tests can use real Linux facilities.
- Checkpoint backends do not own UI state or scheduling policy.
- SQLite metadata references checkpoint generations stored outside the database.

## Verification boundaries

- Unit tests validate portable logic in isolation.
- Integration tests validate multiple NapGuard components together.
- System tests execute against real Ubuntu services, kernel facilities, and checkpoint engines.
- A mock, replay, or injected signal is never evidence that the corresponding physical event works.

