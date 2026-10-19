# IPC and Security Design

- Status: Stage 01 design, implemented in Stage 06 (IPC). Security controls are implemented in the stage of each owning component.
- Related: [components](components.md), [storage and recovery](storage-recovery.md), [ADR 0002](../decisions/0002-ipc-protocol-and-authentication.md), [requirements](../requirements/requirements.md)

## Threat model

| Actor | In scope? | Control |
|---|---|---|
| Another local user | yes | runtime directory 0700, socket 0600, `SO_PEERCRED` UID check (SEC-001, SEC-002), data directory 0700 (SEC-006) |
| Malicious or foreign checkpoint files dropped into the data directory | yes | catalog provenance and digest checks, no import path (SEC-007), permission and symlink checks (SEC-006, SEC-011) |
| Malformed or hostile client input from the same user's processes | yes, for robustness | schema validation, size limits, no shell (SEC-004, SEC-005) |
| A same-UID process deliberately misusing NapGuard | partially | any same-UID process can already ptrace or signal the user's processes, so NapGuard adds no new authority beyond its documented API. Audit logging still records the requests (SEC-009). |
| root | no | root can bypass every user-level control |

## Transport

- `AF_UNIX`, `SOCK_STREAM`, at `$XDG_RUNTIME_DIR/napguard/daemon.sock`.
- There is never a TCP or abstract-namespace listener.
- **Startup checks:**
  - `$XDG_RUNTIME_DIR` exists, is owned by the user, and has mode 0700.
  - `napguard/` is created with mode 0700, or verified if it already exists.
  - Any stale socket is removed only after `flock(daemon.lock)` is held.
  - The socket is bound with umask 077 and then `chmod 0600`.
  - Any check that fails gives `INSECURE_PATH` and the daemon exits (SEC-001).
- **Framing:** a 4-byte unsigned big-endian length, then that many bytes of UTF-8 JSON (a single object). The maximum frame is 1 MiB. A larger frame gets a `FRAME_TOO_LARGE` error and the connection is closed (SEC-005).
- **Limits:**
  - At most 16 connections.
  - At most 32 outstanding requests per connection; beyond that, `BUSY`.
  - An idle connection without a subscription is closed after 300 s.

## Authentication and peer credentials (SEC-002)

- **On accept:** read `SO_PEERCRED` (`struct ucred {pid, uid, gid}`) with `getsockopt(SOL_SOCKET, SO_PEERCRED)`. If `uid != os.getuid()`, send one `UNAUTHENTICATED` error frame, close the connection, and emit an `AUTH_REJECTED` security event with the peer's pid and uid. This rejects every non-matching UID, including root.
- **Recorded in audit events:** the peer pid and uid. The pid is informational only and never used for authorization, because it can be reused.
- **No tokens or passwords:** filesystem permissions plus kernel peer credentials are the authentication. Rationale: [ADR 0002](../decisions/0002-ipc-protocol-and-authentication.md).

## Envelopes

The request:

```json
{"v": 1, "id": "c-17", "method": "checkpoint.request",
 "params": {"workload_id": "…"}}
```

A successful response, and a failed one:

```json
{"v": 1, "id": "c-17", "ok": true, "result": {"operation_id": "…", "state": "ACCEPTED"}}
{"v": 1, "id": "c-17", "ok": false,
 "error": {"code": "CONFLICT", "message": "Workload has a live instance",
           "action": "Stop the workload before restoring", "retryable": false,
           "details": {"instance_id": "…"}}}
```

An event, sent after `events.subscribe`:

```json
{"v": 1, "event": "checkpoint.published", "seq": 1042, "ts": "…", "simulated": false,
 "data": {"workload_id": "…", "checkpoint_id": "…", "generation": 42}}
```

- `v` is the protocol version. An unknown version gets `UNSUPPORTED_VERSION`.
- `id` is a client-chosen string of 1–64 characters, echoed back in the response.
- Unknown top-level or parameter fields are rejected with `INVALID_ARGUMENT` (SEC-005).
- Events carry a monotonically increasing `seq`, so a reconnecting client can catch up with `events.query(after_seq)`.

## Methods (v1)

| Method | Kind | Purpose |
|---|---|---|
| `daemon.status` | sync | daemon state, version, uptime |
| `daemon.capabilities` | sync | backend(s), battery, sleep, and provider (real or simulated) |
| `workload.register` / `update` / `remove` / `get` / `list` | sync | registry (FR-001 – FR-004) |
| `workload.assess` | async | compatibility assessment (FR-005) |
| `workload.start` / `stop` / `restart` | async | supervisor (FR-007 – FR-009, FR-028) |
| `policy.get` / `policy.set` | sync | protection policy |
| `checkpoint.request` | async | manual checkpoint (FR-010) |
| `checkpoint.list` / `get` | sync | history (FR-012) |
| `checkpoint.verify` / `delete` | async | integrity check, explicit deletion |
| `recovery.candidates` | sync | recovery points (FR-022) |
| `recovery.restore` | async | requires `confirm: true` (FR-023) |
| `operation.get` / `cancel` | sync | operation status and cancellation |
| `events.subscribe` / `unsubscribe` / `query` | sync, then stream | events (FR-033) |

## Error codes

Codes are stable: once released they are never renamed or reused. Every code has a message and an action entry (UX-006).

| Code | Meaning |
|---|---|
| `INVALID_ARGUMENT` | schema or validation failure (with field details) |
| `UNKNOWN_METHOD`, `UNSUPPORTED_VERSION`, `FRAME_TOO_LARGE`, `BUSY` | protocol-level errors |
| `UNAUTHENTICATED` | peer UID mismatch |
| `NOT_FOUND` | unknown workload, checkpoint, or operation ID |
| `CONFLICT` | the workload is active, a duplicate restore, or a stale `config_version` |
| `INVALID_STATE` | the transition is not allowed by the state machine |
| `NOT_RUNNING`, `PROCESS_GONE` | no live managed process |
| `BACKEND_UNAVAILABLE`, `UNSUPPORTED_WORKLOAD`, `PERMISSION_DENIED`, `RESOURCE_CONFLICT` | backend-level errors ([backend contract](checkpoint-backend.md#error-contract)) |
| `LAUNCH_FAILED`, `CHECKPOINT_FAILED`, `RESTORE_FAILED`, `VERIFY_FAILED` | operation failures |
| `TIMED_OUT`, `CANCELLED`, `INTERRUPTED` | operation ended without completing |
| `INSUFFICIENT_SPACE` | quota or free-space failure (REL-010) |
| `INVALID_ARTIFACTS`, `INVALID_MANIFEST`, `INSECURE_ARTIFACT`, `UNTRUSTED_CHECKPOINT`, `CORRUPT` | storage and trust failures |
| `ARCH_MISMATCH`, `MACHINE_MISMATCH`, `VERSION_MISMATCH` | restore compatibility failures |
| `STORAGE_ERROR` | an unexpected I/O or catalog error |
| `INTERNAL` | a bug; logged with a traceback |

Daemon startup failures, reported through the exit status and journald rather than IPC: `INSECURE_PATH`, `STORAGE_NOT_PERSISTENT`, `PLATFORM_UNSUPPORTED`, `ALREADY_RUNNING`, `CATALOG_CORRUPT`, `CATALOG_MIGRATION_FAILED`. The client-only error is `DAEMON_UNAVAILABLE`.

## Input validation (SEC-005, FR-002)

- **Schemas:** a strict per-method schema, written as typed dataclasses with explicit validators, with no new dependency unless approved by ADR (NFR-002).
- **Strings:** must be valid UTF-8 with no NUL. Names have at most 64 characters, paths at most 4096 bytes, and argument lists at most 256 items of at most 4096 bytes each.
- **Paths:** must be absolute. They are normalized with `os.path.realpath` and then checked against FR-002. A symlinked executable is resolved and the resolved target is validated and stored.
- **Environment:** variable names must match `[A-Za-z_][A-Za-z0-9_]*`, with at most 64 variables, and values at most 32 KiB in total.
- **Integers:** ranges are checked against the [data model](data-model.md) constraints. UUIDs must be canonical lowercase.

## Process ownership checks (SEC-003, EXC-002)

- The API accepts workload IDs only, never a raw PID.
- **Before signalling, checkpointing, or restoring,** the target instance's identity is confirmed by all of these:
  - `os.pidfd_open(pid)` succeeds;
  - `/proc/<pid>/stat` field 22 equals the recorded `pid_start_time`;
  - `/proc/<pid>/status` shows the user's `Uid`;
  - the boot ID matches.

  If any check fails, the result is `PROCESS_GONE`.
- Signals are sent with `signal.pidfd_send_signal` on the held pidfd, which cannot target a reused PID.
- Executables with the setuid or setgid bit are refused at registration (EXC-002).

## Safe subprocess execution (SEC-004)

- Processes are started with `asyncio.create_subprocess_exec(*argv)`. A shell is never used, and neither are `shell=True`, `os.system`, nor string commands.
- The environment is built from the policy allow-list (SEC-008). The daemon's own environment is not inherited.
- Spawn settings:
  - `cwd` is the validated working directory.
  - stdin is `/dev/null`; stdout and stderr go to size-capped per-instance log files with mode 0600.
  - `close_fds=True`, `start_new_session=True`, and umask 077.
- Backend tools are invoked through the absolute path recorded by `probe`, never resolved through `PATH` at call time (PLT-006).
- Every call has a timeout (REL-008). When a timeout fires, the helper subprocess's process group is killed. The workload's group is not.

## Checkpoint file permissions (SEC-006)

- Directories are 0700 and files 0600, owned by the daemon's UID, with umask 077 for the daemon and all backend subprocesses. Before trusting an artifact, NapGuard checks ownership, mode, and that it is not a symlink.
- Checkpoint images contain process memory, which can include passwords, keys, and tokens the workload held. The registration flow and the documentation warn about this (SEC-008). The data directory must not be placed on shared or removable media. Encryption at rest is out of scope for v1 (OD-06).

## Trust and restore restrictions (SEC-007)

- A checkpoint may be restored only if it was published by this installation's catalog, its manifest digest matches, it belongs to this user, and it passes the integrity and host checks ([storage design](storage-recovery.md#integrity-checks)).
- No API imports, uploads, or registers external checkpoint directories. Unknown directories are quarantined.
- Restores run as the same user. No privilege change happens during a restore.

## Audit logging (SEC-009)

- **Recorded:** security-category `SystemEvent`s for authentication rejections, workload registration, update, and removal, policy changes, signals sent (with the signal name), checkpoint deletion, restore requests and results, integrity failures, quarantined directories, and unsafe path refusals.
- **Each event includes:** timestamp, peer uid and pid when IPC-initiated, workload, operation, and attempt IDs, and the result.
- **Never logged:** environment values, full argument lists (only their SHA-256), or file contents.
- **Destinations:** the catalog's event table, which follows the event retention policy, and the JSON log stream to journald. journald keeps its own copy outside NapGuard's control.
