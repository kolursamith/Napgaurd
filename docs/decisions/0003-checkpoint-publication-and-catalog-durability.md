# ADR 0003: Checkpoint Publication and Catalog Durability

- Status: Proposed (Stage 01). To be confirmed in Stage 04 with fault-injection tests on Ubuntu.
- Date: 2026-10-19
- Requirements: REL-001 – REL-006, REL-010, REL-011, SEC-007

## Context

A power loss must never cause an incomplete checkpoint to be advertised (REL-001). Large binary images do not belong in SQLite (ADR 0001). The filesystem and the catalog therefore have to be kept consistent across a crash at any point.

## Decision

1. Backends write into a per-operation staging directory on the same filesystem as `published/`.
2. Before publication, NapGuard validates every artifact and records its SHA-256. It then fsyncs every file, every directory, and the manifest.
3. A catalog row in state `PUBLISHING` records the intent before the atomic `rename(2)` into `published/`. After the rename, the parent directories are fsynced.
4. **The commit point is the catalog transaction that sets `PUBLISHED`.** Startup reconciliation rolls an interrupted `PUBLISHING` checkpoint forward only after fully re-verifying it. Every other incomplete state is discarded.
5. SQLite runs in WAL mode with `synchronous=FULL`, through a single writer connection.
6. The catalog stores the manifest's digest. Only catalog-published, digest-matching checkpoints are trusted, and external import is not supported.

The full protocol and its crash table are in the [storage and recovery design](../architecture/storage-recovery.md#atomic-publication-protocol).

## Alternatives considered

- **Storing images in SQLite:** simple atomicity, but images can be gigabytes, and it would bloat the WAL and slow the catalog. Rejected.
- **Manifest-only commit, with the catalog rebuilt from disk:** the catalog would no longer be the authority on trust, which weakens SEC-007. Rejected.
- **`synchronous=NORMAL`:** faster, but in WAL mode the most recent transactions can be lost on power loss. Rejected for catalog state changes.

## Consequences

- Each publication costs several fsyncs. This is measured as `validate_ms` (PRF-001).
- Durability is only as good as the storage stack's honouring of flushes. Under UTM/QEMU, the virtual disk's cache mode affects whether a host-level failure loses flushed data. REL-007 tests a guest power-off, not host failure (risk R-05 in the [Stage 01 report](../phases/STAGE-01-report.md)).
