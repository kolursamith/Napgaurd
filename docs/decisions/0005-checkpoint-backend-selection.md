# ADR 0005: Checkpoint Backend Selection (Provisional)

- Status: **Proposed (draft for review).** It closes open decision OD-01 provisionally.
- Date: 2026-10-20
- Related: [Stage 02 report](../phases/STAGE-02-report.md) (sections B, C, I, N, S, T, U), [ADR 0004](0004-stage02-dmtcp-loopback-coordinator-exception.md), [backend contract](../architecture/checkpoint-backend.md)
- Requirements: CMP-001, CMP-002, SEC-002, SEC-010, PLT-001, PLT-009, LIM-003

## Context

Stage 02 had to establish whether any backend can genuinely checkpoint and restore an unmodified process on the target, Ubuntu 24.04 aarch64 in UTM.

- **DMTCP 4.2.0** was built from pinned source as an ordinary user. In one controlled trial it checkpointed and restored the `c-single` workload **in the same boot**. The restored process showed the original nonce and resumed from the checkpoint window (counter 40, after termination at 450), then advanced. Evidence: Stage 02 report section T (user-reported).
- **CRIU 4.2.1** was not run. As an ordinary user under Ubuntu's Yama `ptrace_scope=1`, it needs `CAP_CHECKPOINT_RESTORE` and `CAP_SYS_PTRACE` on its binary, or root. Either option is root-equivalent and conflicts with SEC-010 (OD-14).

## Decision (proposed)

**DMTCP 4.2.0 is the provisionally selected primary backend.** It is the only backend with real Ubuntu ARM64 evidence of genuine checkpoint/restore that works without privileges. **CRIU remains the alternative.** It needs further privilege-feasibility work: a reviewed capability or helper design, approved by its own ADR.

The selection is **provisional and scoped**:

- It is verified for `c-single`, same boot, one trial.
- Cross-reboot restore and the other workload classes are not verified (Stage 02 report section U).
- Support claims follow CMP-002: only for (DMTCP 4.2.0, aarch64, a verified class).

## DMTCP is not production-safe

These findings stand and must be resolved, or explicitly accepted by a later ADR, before any production use (Stages 06 and later):

1. **Unauthenticated coordinator.** By default it listens on all interfaces (`JSockAddr::ANY`). Even confined to loopback, any local user can connect and command it (kill, checkpoint), which violates SEC-002 on multi-user hosts.
2. **Undocumented fd-821 hand-off.** Loopback confinement relies on DMTCP's internal `PROTECTED_COORD_FD` convention. It is fail-closed in the harness, but it is not a supported interface and must be re-verified for any DMTCP version (pinned to 4.2.0).
3. **TCP restore limitation (I-1c).** A restored image that contains workload IPv4 or IPv6 sockets makes `dmtcp_restart` open an `INADDR_ANY` restore listener (`connectionrewirer.cpp:173-207`). Socket-holding workloads are therefore excluded (LIM-003).
4. **`--bcheckpoint` does not block** in 4.2.0. Completion must be verified independently (Stage 02 report section S).
5. **ARM64 is not CI-tested upstream**, and the restart path has an aarch64 TODO (B4, B5).

## Alternatives

| Alternative | Status |
|---|---|
| CRIU with a capability-granted binary | needs an ADR and approval; root-equivalent capability for whoever can run the binary |
| A privileged CRIU helper service (root, narrow authenticated IPC) | changes SEC-010; needs a design and an ADR |
| An x86-64 target, where DMTCP is CI-tested | changes PLT-001; needs separate verification |
| Patching DMTCP (bind address, coordinator authentication) | possible future work; third-party fork maintenance |

## Consequences

- **Stage 03** (backend integration) builds the DMTCP adapter behind the backend-neutral contract, with ADR 0004's conditions carried into a default-refusing `CoordinatorPolicy`. It needs its own ADR for developer use, because ADR 0004 is Stage 02-only.
- CRIU stays probe-only until OD-14 is decided.
- This ADR is revisited when cross-reboot evidence exists, when more classes are verified, or if DMTCP fails any of them.
