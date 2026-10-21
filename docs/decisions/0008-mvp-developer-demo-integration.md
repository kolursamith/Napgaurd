# ADR 0008: MVP Developer-Demo Integration of Stages 04–10

- Status: **Proposed. Owner acceptance requested.** It records how the rapid MVP extends, and in places deviates from, earlier decisions. It does **not** authorize production use.
- Date: 2026-10-21
- Related: [ADR 0003](0003-checkpoint-publication-and-catalog-durability.md), [ADR 0006](0006-stage03-dmtcp-backend-integration-and-coordinator-security.md), [ADR 0007](0007-synchronous-backend-calls-in-stage-03a.md), [MVP report](../phases/MVP-report.md)
- Requirements: SEC-001, SEC-002, SEC-005, SEC-007, FR-010, FR-013, FR-014, FR-019, FR-023, FR-025, FR-026, FR-030, LIM-007, EXC-009

## Context

Stage 03a was verified on Ubuntu ARM64 at commit `b12ca96d5a3cecc69ea15ed393cf3153d0da7cd6`: a genuine DMTCP checkpoint, original termination, and a genuine restore with verified continuity, no network exposure, and no leftovers. The owner asked for a demonstrable MVP of Stages 04–10 on an urgent schedule.

ADR 0006 allows DMTCP coordinators only in Stage 03a developer mode, and says the daemon must not start them until a later ADR removes the local-user exposure of the unauthenticated coordinator. That exposure is **not** removed.

## Decision (proposed)

1. **Coordinators in the daemon, developer mode only.**
   - The daemon starts DMTCP coordinators only if the daemon process itself was started with `--i-accept-single-user-coordinator`. The acknowledgement covers that daemon run only and is never stored.
   - Every ADR 0006 condition stays in force through the unchanged Stage 03a adapter: pinned DMTCP 4.2.0, a single-user host, the loopback hand-off, audits, and the fail-closed rules.
   - Without the flag, launch, checkpoint, and restore are refused, and status, history, and verification still work.
   - `DMTCP_LOOPBACK_ONLY_COORDINATOR_VERIFIED` stays `False`.
   - The daemon reports `production_authorized: false`.
2. **Workloads.** Only NapGuard's verified reference classes `c-single` and `c-threads` can be launched. External processes are never attached (EXC-008).
3. **Images are not relocated** (deviation from ADR 0003's staging-to-`published/` rename).
   - Images stay where DMTCP wrote them, as in the verified Stage 03a path.
   - The catalog stores the manifest's SHA-256 as the root of trust (SEC-007). Every restore re-verifies the manifest and every artifact hash.
   - DMTCP overwrites an instance's image at each checkpoint, so **only the newest generation of an instance is restorable**; older catalog rows become `SUPERSEDED` (history, not restorable).
4. **No checkpoint of a restored instance.** Where DMTCP writes such an instance's next image is unverified, so the MVP refuses with `INVALID_STATE`.
5. **The daemon owns its sessions** (deviation from FR-030). Stopping the daemon stops its managed workloads and coordinators. A restarted daemon marks earlier instances `LOST` and stops leftover, identity-matched processes of them.
6. **Restore success is verified:**
   - the restored process belongs to this restore and stays alive through the verification window (FR-026);
   - it shows the original nonce, with a counter rolled back into the checkpoint window and advancing.

   Otherwise the restore is `FAILED`.
7. **Battery.** Readings come only from sysfs. A simulated reading exists for demonstrations; it is labelled `SIMULATED` everywhere, and every event and trigger it causes is flagged simulated (EXC-009).
8. **Operations** run one at a time on a single worker. A second checkpoint for the same workload is merged (FR-014). Requests return an operation ID at once (FR-010). Priority ordering (FR-016) is FIFO in the MVP.
9. **IPC** follows ADR 0002:
   - a 0700 runtime directory and a 0600 socket, with `flock` and `SO_PEERCRED`;
   - 4-byte length-prefixed JSON frames of at most 1 MiB;
   - per-method schemas.

   There is no TCP API.

## Consequences

- An end-to-end demo works on the verified platform: GUI or CLI → daemon → Stage 03a adapter → DMTCP.
- These requirements are not met by the MVP:
  - **FR-030:** workloads stop with the daemon;
  - **FR-016:** operations run in FIFO order, without priorities;
  - **REL-007:** cold-boot recovery is not implemented;
  - **ADR 0003:** checkpoints are not published into `published/`;
  - **retention (REL-011):** not implemented.
- The local-user exposure of the coordinator (ADR 0006 §2) remains the blocker for any production use.
