# NapGuard Implementation Status

Status as of 2026-10-22. "Verified on Ubuntu" means the owner ran it on Ubuntu 24.04.5 ARM64 and reported a PASS for that commit. Nothing is marked verified from Mac tests.

| Feature | Status | Evidence |
|---|---|---|
| DMTCP 4.2.0 checkpoint/restore of `c-single` (same boot and cross-reboot) | **Verified on Ubuntu** (Stage 02) | [Stage 02 report](phases/STAGE-02-report.md#ab-final-stage-02-evidence-compatibility-matrix-and-closure-readiness) |
| `c-threads`, `c-file-static`, `c-file-grow` restore | **Verified on Ubuntu** (Stage 02) | same |
| Python workloads | **Known failure** (2 of 2 restores; SIGSEGV), refused | same |
| `c-child`, TCP, GUI workloads | **Unsupported** (blocked by policy) | same |
| Stage 03a adapter and self-test through NapGuard's API | **Verified on Ubuntu** at `b12ca96d` | owner report |
| MVP daemon, CLI, catalog, real checkpoint, verify, restore, continuity | **Verified on Ubuntu** at `43832c3` (166 Linux tests passed). The shutdown step failed in that run; fixed in `b3e6980`, which has not been re-verified. | [MVP report](phases/MVP-report.md) |
| Restore ownership fix for a report written mid-checkpoint (GUI restore failure) | Implemented (`fdf3c1a`); **awaiting Ubuntu verification** | MVP report |
| Scheduler (automatic checkpoints) | Implemented; **awaiting Ubuntu verification** | final script step 9 |
| Battery: sysfs reading, unavailable in a VM | Implemented; awaiting Ubuntu verification | final script step 10 |
| Battery: SIMULATED readings and triggers | Implemented (labelled simulation); awaiting Ubuntu verification | same |
| Redesigned GUI (8 screens and the guided demo) | Implemented; **awaiting Ubuntu verification** (the GUI cannot run on the development Mac) | final script step 7 (`gui --smoke`) |
| Archive, history deletion, reset demo, checkpoint deletion | Implemented with Mac regression tests; awaiting Ubuntu verification | final script steps 11–12 |
| Catalog migration v1 → v2 (archive columns) | Implemented, tested on the Mac; additive and non-destructive | `tests/unit/test_final_history.py` |
| Install, uninstall, desktop launcher, icon | Implemented, tested in a sandbox on the Mac; awaiting Ubuntu verification | final script step 4 |
| systemd user unit | **Experimental**: runs in refused mode (the developer acknowledgement is never stored) | [ADR 0008](decisions/0008-mvp-developer-demo-integration.md) |
| Production coordinator security (authenticated) | **Unsupported**: not production-authorized | [ADR 0006](decisions/0006-stage03-dmtcp-backend-integration-and-coordinator-security.md) |
| Re-checkpointing a restored instance | **Unsupported** in this version | ADR 0008 |
| Cold-boot automatic recovery, retention/quotas, suspend integration | **Not implemented** | [Known Limitations](KNOWN_LIMITATIONS.md) |

**Mac validation of this release:** pytest, Ruff, strict mypy, shell syntax checks, documentation consistency, and sandboxed packaging tests. Linux-only tests (real `/proc`, `ss`, `SO_PEERCRED`, GUI smoke) run only in `scripts/final-ubuntu-check.sh`.
