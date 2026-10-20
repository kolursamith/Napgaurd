# ADR 0007: Synchronous Backend Calls with Explicit Deadlines in Stage 03a

- Status: **Proposed. Owner approval requested.** It records a deviation from the Stage 01 design sketch. It changes no security property of [ADR 0006](0006-stage03-dmtcp-backend-integration-and-coordinator-security.md).
- Date: 2026-10-20
- Related: [backend contract](../architecture/checkpoint-backend.md), [Stage 03 plan](../phases/STAGE-03-plan.md)
- Requirements: REL-008, NFR-004

## Context

The Stage 01 [backend contract](../architecture/checkpoint-backend.md) sketched `CheckpointBackend` with `async def` methods. Stage 03a has no event loop:

- its only caller is the `napguard-dev` developer CLI;
- every backend step is a blocking subprocess call (DMTCP tools, `ss`, `ip`) or a bounded poll of the filesystem and `/proc`;
- the daemon that would own an event loop arrives in Stage 06.

Declaring the methods `async` now would not make them non-blocking. Each body would still block the loop.

## Decision (proposed)

1. **Stage 03a implements the contract synchronously.** Method names, arguments, data types, and error codes follow the contract. Every call takes an explicit `deadline` (a `time.monotonic()` value) instead of relying on async cancellation.
2. **Deadlines are enforced inside the adapter** (REL-008):
   - every subprocess gets a timeout derived from the deadline;
   - a passed deadline raises `BackendError(TIMED_OUT)`;
   - a timed-out checkpoint leaves the workload running.
3. **Stage 06 integration.** The daemon calls the backend through `asyncio.to_thread`, at most one call per workload (FR-014). Cancellation stays `NOT_SUPPORTED` for DMTCP, as the contract allows.
4. **Contract updates.** The contract document keeps the async sketch and notes this ADR. It is revisited in Stage 06 if a native async adapter is needed.

## Consequences

- The developer CLI and the tests are simpler, and timing behaviour is explicit and testable with injected clocks.
- A Stage 06 thread wrapper must not call two operations on one session concurrently. The checkpoint manager's per-workload serialization (FR-014) already requires that.
- **No security change.** Coordinator policy, isolation, audits, process ownership, and artifact validation are unaffected.
