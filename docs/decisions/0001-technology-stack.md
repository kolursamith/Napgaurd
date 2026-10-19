# ADR 0001: Proposed Technology Stack

- Status: Proposed; Linux feasibility gates remain open
- Date: 2026-10-11

## Context

NapGuard needs a desktop UI, a persistent background coordinator, durable metadata, local IPC, Linux power and sleep integration, and one or more checkpoint backends. The development host is macOS, while the intended product and integration-test target is Ubuntu 24.04.

## Decision

### Python 3.12+

Use typed Python for orchestration, policy, storage, IPC, and UI code. Python supports fast iteration, asyncio, SQLite in the standard library, mature testing tools, and PySide6. The project baseline is Python 3.12; newer interpreters may be used only when dependencies remain compatible.

### PySide6 / Qt

Use PySide6 for the future Linux desktop application because Qt has mature Linux desktop integration and Python bindings. PySide6 is not a Layer 0 dependency and will be installed only when the UI layer is approved.

### DMTCP as the first checkpoint candidate

DMTCP is proposed because it is designed for transparent user-space checkpoint/restart of unmodified processes. It is not assumed to support arbitrary applications. Layer 1 must test representative workloads on the verified Ubuntu architecture before any support claim.

### Optional CRIU backend

CRIU may complement DMTCP when kernel capabilities, privileges, namespaces, and workload characteristics permit. It remains optional and must pass separate feasibility and security review.

### SQLite metadata

Use SQLite for local structured metadata and transactional updates. Store large checkpoint artifacts in the filesystem rather than database blobs, and coordinate database state with durable generation commits.

### systemd user service

Run the future daemon as a user-level systemd service where feasible. This provides lifecycle management and restart policy while avoiding root by default. The approach must be verified on Ubuntu.

### Unix-domain sockets

Use a local Unix-domain socket for daemon IPC. Filesystem permissions and peer credentials can constrain access without exposing a network listener. The protocol and authentication design belong to a later approved layer.

## Consequences

- Portable Python checks can run on macOS, but Linux integration cannot be certified there.
- PySide6 and checkpoint tools are deferred, keeping Layer 0 dependencies minimal.
- DMTCP and CRIU compatibility will be empirical and workload-specific.
- Durable checkpoint commits require explicit filesystem semantics beyond SQLite transactions.
- systemd, D-Bus, sysfs, and checkpoint engines require real Ubuntu system tests.

## Validation gates

1. Layer 0 must verify a usable Python toolchain and intended Ubuntu access.
2. Layer 1 must run real DMTCP checkpoint/restart experiments and record a compatibility matrix.
3. CRIU remains disabled until its target-host requirements and privilege model are verified.
4. Later layers must validate Unix-socket authorization, SQLite recovery, filesystem durability, systemd integration, and PySide6 packaging independently.
