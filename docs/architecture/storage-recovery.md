# Storage and Recovery Design

- Status: Stage 01 design. Implemented in Stage 04 (storage) and Stage 09 (recovery).
- Related: [data model](data-model.md), [components](components.md), [backend contract](checkpoint-backend.md), [requirements](../requirements/requirements.md)

## Directory structure

Paths below are the defaults (PKG-006). `$XDG_DATA_HOME` defaults to `~/.local/share`.

```text
$XDG_DATA_HOME/napguard/                       0700
  catalog.sqlite3  (+ -wal, -shm)              0600
  checkpoints/                                 0700
    <workload_id>/                             0700
      staging/                                 0700
        <operation_id>/                        0700   in progress, never advertised
          artifacts/...                        written by the backend
      published/                               0700
        g000042-<checkpoint_id>/               0700   one published checkpoint
          manifest.json                        0600
          artifacts/...                        0600 files, 0700 dirs
      quarantine/                              0700
        <name>-<utc-timestamp>/                corrupt or unknown dirs; kept for inspection
  logs/<workload_id>/<instance_id>.{out,err}   0600, size-capped
$XDG_CONFIG_HOME/napguard/config.toml          0600
$XDG_RUNTIME_DIR/napguard/                     0700   daemon.sock 0600, daemon.lock 0600
```

- `staging/`, `published/`, and `quarantine/` live under the same workload directory, so they are on the same filesystem and `rename(2)` between them is atomic.
- At startup the daemon checks that the data directory is not on tmpfs or ramfs (PLT-007).
- At startup it also checks ownership and modes along the whole path, and refuses to start on a mismatch (SEC-001, SEC-006).

## SQLite catalog

- **Tables:** `workload`, `protection_policy`, `process_instance`, `checkpoint`, `checkpoint_operation`, `recovery_attempt`, `system_event`, and `schema_meta`. Fields, constraints, and relationships are in the [data model](data-model.md).
- **Pragmas, set and read back at every open (REL-003):**
  - `journal_mode=WAL`
  - `synchronous=FULL`, so a committed transaction survives power loss. `NORMAL` is not durable in WAL mode for the last commits.
  - `foreign_keys=ON`
  - `busy_timeout=5000`
- **Schema version:** `PRAGMA user_version` holds it. Migrations are forward-only and numbered, and each runs in a transaction. If a migration fails, the daemon reports `CATALOG_MIGRATION_FAILED` and does not serve.
- **Writes:** a single writer connection owned by `storage`. Read-only connections are allowed for queries.
- **Integrity:** `PRAGMA quick_check` runs at every startup. A non-`ok` result is fatal (`CATALOG_CORRUPT`). The catalog is never silently recreated, and the user is told how to restore from a backup copy.
- **Backup:** before every migration, NapGuard writes a copy with `VACUUM INTO 'catalog.backup-<version>.sqlite3'`.

## Manifest

`manifest.json` is UTF-8 JSON with sorted keys and at most 1 MiB:

```json
{
  "format": "napguard-checkpoint-manifest",
  "format_version": 1,
  "checkpoint_id": "…", "workload_id": "…", "operation_id": "…", "instance_id": "…",
  "generation": 42,
  "trigger": "PERIODIC",
  "created_at": "2026-10-19T09:30:00.000Z",
  "backend": {"name": "dmtcp", "version": "…", "restore_hints": {}},
  "host": {"arch": "aarch64", "kernel_release": "…", "os_id": "ubuntu",
           "os_version_id": "24.04", "boot_id": "…", "machine_id_sha256": "…"},
  "workload": {"executable": "/abs/path", "argv_sha256": "…", "working_dir": "/abs",
               "env_policy_sha256": "…", "config_version": 3},
  "process": {"pid": 1234, "pid_start_time": 567890},
  "artifacts": [
    {"path": "artifacts/ckpt_x.dmtcp", "role": "IMAGE", "size": 12345678,
     "sha256": "…", "mode": "0600"}
  ],
  "total_bytes": 12345678,
  "durations_ms": {"capture": 850, "validate": 120}
}
```

- The manifest does not contain its own digest. The catalog stores `manifest_sha256` (SEC-007).
- Artifact paths must be relative, normalized, inside the checkpoint directory, and must name regular files (SEC-011).
- Argument and environment values are stored as hashes. Their full values live only in the catalog.

## Atomic publication protocol

For each checkpoint operation, with `W` = the workload directory:

1. **Preflight.** Run the free-space check (see quotas below). Insert a `checkpoint` row in state `STAGING` with the next `generation`, which reserves the number.
2. **Staging.** `mkdir W/staging/<op>` and `W/staging/<op>/artifacts` with mode 0700, then `fsync(W/staging)`.
3. **Capture.** The backend writes into `artifacts/` (backend contract rule 2). On success the row becomes `CAPTURED`.
4. **Validate.** List the directory without following symlinks. Every file must be a regular file owned by the user. For each artifact:
   1. Set its mode to 0600.
   2. Compute SHA-256 and size, streaming the file.
   3. `fsync` the file.

   Then `fsync` every directory under `artifacts/`, deepest first. The artifact list must match the backend's reported `CaptureResult.artifacts` exactly; otherwise the capture is rejected with `INVALID_ARTIFACTS`.
5. **Manifest.** Write `manifest.json.tmp`, `fsync` it, `rename` it to `manifest.json`, then `fsync(W/staging/<op>)`.
6. **Intent.** In one transaction, set `state=PUBLISHING`, `relative_dir=<W>/published/g<generation>-<id>`, and `manifest_sha256`.
7. **Publish.** `rename(W/staging/<op>, W/published/g…-<id>)`, then `fsync(W/published)` and `fsync(W/staging)`.
8. **Commit point.** In one transaction, set `state=PUBLISHED` and `published_at`, set the operation to `SUCCEEDED`, and append the `checkpoint.published` event. **Only now is the checkpoint advertised (REL-001).**
9. **Retention.** Apply retention (below) in a separate step, after the commit.

Crash and power-loss boundaries, always resolved during startup reconciliation (REL-005):

| Crash after step | On-disk state | Catalog state | Reconciliation |
|---|---|---|---|
| 1–4 | partial staging directory | `STAGING`/`CAPTURED` | `FAILED (INTERRUPTED)`, remove the staging directory, operation `INTERRUPTED` |
| 5–6 | complete staging directory | `CAPTURED` or `PUBLISHING` | If `CAPTURED`: as above. If `PUBLISHING` and the final directory is absent: `FAILED`, remove staging. |
| 7 (rename done) | final directory present | `PUBLISHING` | Fully re-verify the manifest digest and all hashes. If OK, roll forward to `PUBLISHED` and log it. Otherwise `CORRUPT` and quarantine it. |
| 8 | final directory present | `PUBLISHED` | nothing to do |
| any | directory under `published/` with no row | — | quarantine it, log `storage.unknown_dir`, never advertise it |
| any | `PUBLISHED` row, directory missing | `PUBLISHED` | `MISSING`, log `storage.missing` |

The previous `PUBLISHED` checkpoint is untouched by steps 1–8, which satisfies REL-004.

## Integrity checks

| Check | When | Failure result |
|---|---|---|
| Hashes, sizes, and file set match the manifest | at publication (step 4), before every restore (REL-006), and on demand (`checkpoint.verify`) | `CORRUPT` → quarantine, older checkpoint offered |
| Manifest digest equals the catalog's `manifest_sha256` | before every restore | `UNTRUSTED_CHECKPOINT` (SEC-007) |
| Owner is the user, no group or other permission bits, no symlinks | at publication, before every restore | `INSECURE_ARTIFACT` (SEC-006) |
| Architecture, machine, and backend version match | before every restore | `ARCH_MISMATCH`, `MACHINE_MISMATCH`, or `VERSION_MISMATCH` (CMP-004, CMP-005) |

**A passing integrity check does not prove restorability.** Integrity proves only that the bytes are the ones the backend wrote. `restore_verified_at` is set only by a real `SUCCEEDED` recovery (FR-026, FR-006). The GUI and CLI show "integrity OK" and "restore verified" as separate facts.

A trial restore while the original process is alive is not done by default: it would create a duplicate process (FR-025). Stages 02 and 09 prove restorability through controlled kill-and-restore tests.

## Retention policy (REL-011)

Retention runs per workload after each publication. With N = `retention_count`:

1. **Keep set:**
   - the newest N `PUBLISHED` checkpoints by generation;
   - the newest `PUBLISHED` checkpoint that has `restore_verified_at` set, if it isn't already in the newest N;
   - any checkpoint referenced by an active `RecoveryAttempt`.
2. **Expire.** Mark every other `PUBLISHED` checkpoint `EXPIRED` in one transaction.
3. **Delete.** Remove each expired directory. Rename it to `quarantine/.deleting-*` first, then `rmtree` it. Then mark it `DELETED`.
4. **Floor.** Retention never reduces the number of `PUBLISHED` checkpoints below 1.

`CORRUPT` and `MISSING` checkpoints are not counted toward N and are not auto-deleted. The user deletes them explicitly.

## Disk quota handling (REL-010)

- **Global quota:** default 10 GiB, or 50% of the filesystem's available space at first start if that is smaller. Configurable.
- **Per-workload quota:** optional `quota_bytes`.
- **Size estimate** for a new checkpoint: 1.2 × the previous image's `total_bytes`. For a workload's first checkpoint, 1.5 × the workload's total RSS from `/proc/<pid>/status`, summed over its process tree.
- **Preflight passes if all of these hold:**
  - `statvfs` free space ≥ estimate + reserve, where reserve = 1 GiB or 5% of the filesystem, whichever is larger;
  - the estimate fits within the global quota;
  - the estimate fits within the per-workload quota.
- **If preflight fails,** NapGuard first deletes expired, not-yet-deleted checkpoints, then re-checks. If it still fails, the operation fails with `INSUFFICIENT_SPACE` and an event, and the previous checkpoints are left in place.
- **If writing fails** with ENOSPC or EDQUOT during capture or validation, the operation fails with `INSUFFICIENT_SPACE` and the staging directory is removed.
- The last valid checkpoint is never deleted to make room.

## Corrupted and incomplete checkpoints

- **Incomplete** checkpoints are those in `STAGING`, `CAPTURED`, or `PUBLISHING` at a crash. They are never advertised, and reconciliation resolves them as in the table above.
- **Corrupt** checkpoints are those whose hash, manifest, or permission check fails. They are set to `CORRUPT`, their directory is moved to `quarantine/`, and a `storage.integrity_failure` event (category `security`) is logged. They are excluded from recovery candidates, and the next older valid checkpoint is offered.
- **Unknown directories** (on disk with no catalog row) are quarantined and logged, and never imported (SEC-007).

## Recovery-point selection

`recovery.candidates(workload)` returns, newest generation first, every checkpoint that meets all of these:

- `state = PUBLISHED`, and the last integrity result is `OK` (or not yet checked; it will be checked before restore);
- the architecture and machine ID match the current host;
- the backend is available with a compatible version.

Each candidate includes `age`, `trigger`, `restore_verified_at`, the backend, its size, and its limitations. The default selection is the newest candidate. The user may pick an older one. Auto-recovery, when enabled (FR-024), uses only the newest candidate and runs at most once per checkpoint per boot.

## Duplicate restore prevention (FR-025)

Three layers:

1. **Catalog constraints:** at most one active `ProcessInstance` and one active `RecoveryAttempt` per workload (partial unique indexes).
2. **Liveness check:** before restoring, any instance recorded for the current boot must be confirmed dead (pidfd, plus start-time comparison). An instance from a previous boot is dead by definition.
3. **Per-workload lock:** the `asyncio.Lock` serializes start, restore, and stop.

Restoring a workload that has a live process is refused with `CONFLICT`. This stays true even when the user asks to "replace" it: they must stop it first.

## External resources and side effects (LIM-002 – LIM-004, LIM-007)

A restore recreates process state as the backend captured it. It does **not** restore the outside world. The restore dialog and the documentation list these limitations:

- Files the process wrote after the checkpoint keep their newer contents. The restored process may rewrite or append data based on stale assumptions.
- Databases, message queues, and remote services may have moved on. Operations performed after the checkpoint may run again after the restore, such as a second network request or a second database write.
- Network connections, terminals, and device handles may not survive. The actual behaviour is whatever Stage 02 measures for each backend.
- Time jumps forward. Programs that measure wall-clock intervals may misbehave.
- The restored process may get a different PID (LIM-006).

These are inherent to process checkpointing and are why recovery is user-confirmed by default (FR-023).
