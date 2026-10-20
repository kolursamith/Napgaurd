# Stage 03a Report: DMTCP Backend Integration (Developer Mode)

- Date: 2026-10-20
- Branch: `stage-03/backend-integration`, from the Stage 02 closure commit `b7fcddfe582e112cbc69f5ce4078629dd2e06041`
- Authorization: [ADR 0006](../decisions/0006-stage03-dmtcp-backend-integration-and-coordinator-security.md), accepted **only** for Stage 03a developer mode on the isolated single-user Ubuntu 24.04 ARM64 VM
- Verdict: **IMPLEMENTED; NOT PASSED.** Mac validation passes. The Ubuntu gate (genuine checkpoint/restore through NapGuard's API) is **NOT RUN**. Stage 03a must not be called passed until `scripts/stage03-ubuntu-check.sh … --run-selftest` reports PASS on Ubuntu.

## 1. Scope delivered

Every step of the [Stage 03a slice](STAGE-03-plan.md#first-implementation-slice-stage-03a-in-this-order) is implemented and unit-tested on the Mac.

| Module | Purpose |
|---|---|
| `src/napguard/core/proc.py` | Process identity (PID + start ticks + UID + boot ID); pidfd signals; group termination only while the leader is our unreaped child; session-gated adoption; allow-listed environment (SEC-008); private logs; crash-cleanup state file; report-only leak scan |
| `src/napguard/core/netaudit.py` | `ss`/`ip` audits, `ListenerGuard` (baseline diff plus ownership), loopback listener, wildcard-bind refusal; every audit fails closed (`AuditError`) |
| `src/napguard/checkpoint/base.py` | Backend contract: `CheckpointBackend` Protocol, data types, `ErrorCode`, `BackendError`, deadlines |
| `src/napguard/checkpoint/policy.py` | `CoordinatorPolicy`: deny by default; `experimental_single_user` needs the per-invocation flag and a single-user host |
| `src/napguard/checkpoint/compatibility.py` | Reason codes with messages (FR-034); static and runtime fail-closed rules (CMP-006); levels (CMP-002, CMP-003) |
| `src/napguard/checkpoint/artifacts.py` | Artifact validation: flat names, private directory, `O_NOFOLLOW`, owner and permission checks, SHA-256 from the opened descriptor, `CORRUPT` on change |
| `src/napguard/checkpoint/dmtcp_completion.py` | Verified completion wait; code body identical to the Stage 02 `ckptwait.py` |
| `src/napguard/checkpoint/dmtcp.py` | `DmtcpBackend`: probe, assess, launch, status, checkpoint, stop, restore, adopt, audit, cleanup |
| `src/napguard/checkpoint/criu.py` | CRIU **probe only** (`--version`, `getcap`); every operation is refused (OD-14) |
| `src/napguard/selftest/` | Reference workload source (byte-identical to the Stage 02 `ngcounter.c`), state model (code body identical to Stage 02), continuity verifier, self-test runner |
| `src/napguard/cli.py` | `napguard-dev backend probe`, `assess`, `selftest --i-accept-single-user-coordinator`, `cleanup` |
| `scripts/stage03-ubuntu-check.sh` | Ubuntu gate: exact SHA and clean tree; baseline; Stage 03 Linux tests with zero skips; probe; one self-test; listener diff; leftover processes; evidence |

## 2. Review of the earlier unfinished work, and the fixes

| Issue | Finding | Resolution |
|---|---|---|
| Synchronous API versus the async Stage 01 sketch | No event loop exists until Stage 06; `async` bodies would still block | Kept synchronous with an explicit deadline on every call; recorded as [ADR 0007](../decisions/0007-synchronous-backend-calls-in-stage-03a.md) (**Proposed**: needs your approval) |
| The adapter moved images into a separate staging directory | Stage 02 verified restores only from the directory DMTCP wrote to. Moving is plausible but unverified | **Changed:** images are never moved. `checkpoint()` requires `staging_dir` to be the session's DMTCP checkpoint directory (`session.data["checkpoint_dir"]`), and restore reads them there. Publication is Stage 04's job |
| A failed checkpoint deleted final-name images | They can belong to an earlier good capture (REL-004) | **Changed:** only `*.temp` and `*.tmp` are removed |
| The fail-fast restore closed the session twice | Harmless (idempotent) but unclear | **Changed:** closed once, in the error handler |
| The single-user check counted any logind session | A display-manager greeter (`gdm`, a system account) would block the VM | **Changed:** only regular users (UID ≥ 1000, not 65534) count, as ADR 0006 §2 states |

**Security properties re-checked** against Stage 02:

- **Coordinator start:**
  - `CoordinatorPolicy.authorize` runs before anything is bound or spawned;
  - DMTCP must report exactly 4.2.0;
  - the kernel must refuse a wildcard bind next to the loopback listener;
  - the listener is handed over on fd 821 with the same `--coord-port`;
  - the port file must match;
  - `ss` and IPv4/IPv6 reachability probes run.

  Any failure kills the coordinator and closes the listener.
- **Coordinator stop:** `dmtcp_command --quit`, then identity-checked group termination, then the listener is closed. A leak scan follows.
- **Audits:** a baseline is taken before anything starts. Audits run after launch, before and after the checkpoint, on every completion poll, after the restore, and on every self-test wait poll. Exposure, or an audit that cannot run, kills the session and fails the call.
- **Process ownership:** no PID is signalled without a matching identity. Groups are signalled only while their leader is our unreaped child. Crash cleanup skips other boots, other users, and changed identities. The leak scan never kills.
- **Artifacts:** symlinks, group- or other-accessible files, foreign owners, a non-private directory, unsafe names, empty images, and any hash change are rejected **before** a restore starts anything.
- **Developer-only:**
  - only `cli.py` constructs the permissive policy, and only with `--i-accept-single-user-coordinator`;
  - `DmtcpBackend()` defaults to deny;
  - `src/napguard` does not import the Stage 02 harness;
  - `DMTCP_LOOPBACK_ONLY_COORDINATOR_VERIFIED` remains `False`.

## 3. Mac validation (macOS arm64; no DMTCP)

| Check | Result |
|---|---|
| `pytest` (whole repository) | **276 passed, 30 skipped.** All skips are Linux-only tests: 19 from Stage 02, 11 from Stage 03 |
| Stage 03 tests (`tests/unit/test_stage03_*.py`) | 75 tests: 64 passed, 11 skipped (Linux-only) |
| `ruff check .` | pass |
| `mypy` (strict, Linux platform) | pass |
| `bash -n` on every script | pass |
| `git diff --check` | clean |

**Tests by required area:**

| Area | Tests |
|---|---|
| Backend availability, version mismatch | `test_missing_tools_are_unavailable`, `test_version_mismatch_is_unavailable_and_refused`, `test_verified_only_on_the_stage02_platform`, `test_probe_without_dmtcp_is_blocked` |
| Default-deny policy | `test_default_policy_refuses_every_coordinator`, `test_default_policy_refuses_before_anything_starts`, `test_selftest_without_the_flag_is_blocked_and_starts_nothing` |
| Single-user developer mode | `test_developer_mode_needs_a_single_user_host`, `test_host_check_flags_other_users_and_fails_closed_without_logind`, `test_developer_mode_refuses_a_multi_user_host` |
| Fake coordinator attempting a wildcard bind | `test_coordinator_ignoring_the_handoff_fails_closed`, `test_exposed_coordinator_is_killed`, `test_wildcard_bind_is_refused_next_to_a_loopback_listener` (Linux) |
| Listener audit failures | `test_listener_baseline_failure_refuses`, `test_missing_audit_tool_is_an_error`, `test_parse_ss_and_guard_flags_owned_and_new_listeners` |
| Unsupported workloads | `test_unsupported_workload_is_refused_before_launch`, static and runtime rule tests |
| Process identity, PID reuse | `test_forged_identity_and_reused_pid_are_never_signalled`, `test_crash_cleanup_stops_only_identity_matched_processes`, `test_cleanup_stops_only_recorded_identities`, `test_adoption_requires_our_session_and_a_late_start` (Linux) |
| Artifact tampering, symlinks | `test_tampered_artifact_is_corrupt`, `test_symlinked_artifact_is_rejected`, `test_restore_refuses_tampered_or_incompatible_captures` |
| Failed checkpoint and restore cleanup | `test_failed_checkpoint_removes_only_temporary_files`, `test_failed_checkpoint_or_restore_is_fail_and_cleans_up`, `test_checkpoint_timeout_leaves_workload_running_then_cleanup`, `test_failed_restore_reports_diagnostics_and_cleans_up` (Linux) |
| Checkpoint completion timeout | `test_checkpoint_completion_timeout_is_reported`, `test_checkpoint_timeout_leaves_workload_running_then_cleanup` (Linux) |
| Successful continuity (fake backend) | `test_genuine_restore_passes_every_check` |
| No false PASS when restore launches a new process | `test_no_false_pass` (new process, no rollback, surviving original, foreign process, early death), `test_verifier_requires_rollback_window_and_progress` |

The fake backend in `test_stage03_selftest.py` is a **labelled test double**. It proves the self-test's decision logic, not that DMTCP works.

## 4. Deviations from ADR 0006 and the Stage 03 plan

1. **Synchronous calls** instead of the async sketch: ADR 0007, Proposed. No security effect.
2. **Images stay in DMTCP's directory.** The adapter does not move images to a caller-chosen staging directory. This preserves the verified Stage 02 restore path. It is a contract restriction, not a security relaxation, and Stage 04 must design publication around it.
3. **Self-test classes:** `c-single` and `c-threads` only. Both are Stage 02-verified, single-process, and hold no files. `c-file-static` and `c-file-grow` remain `VERIFIED` in assessment but have no self-test.
4. **`CANDIDATE` is not used.** Anything without evidence is `UNKNOWN` (CMP-003).

There is no relaxation of any ADR 0006 security condition.

## 5. Gate (Stage 03a)

| Criterion | Evidence | Result | Remaining risk |
|---|---|---|---|
| DMTCP c-single checkpoint/restore **through NapGuard's API** on Ubuntu ARM64 | `stage03-ubuntu-check.sh <SHA> --run-selftest` | **NOT RUN** | first real use of the adapter; see §6 |
| Capability report correct (DMTCP present, absent, unanalysed version) | unit tests; Ubuntu probe | PASS (Mac unit); Ubuntu **NOT RUN** | — |
| Fail-closed by default (no flag, no coordinator) | unit tests | PASS (Mac) | — |
| No network exposure, no leftover processes | Linux tests and the script's listener diff and `pgrep` | **NOT RUN** (Linux) | — |
| pytest, Ruff, mypy on Ubuntu | baseline step | **NOT RUN** | — |

## 6. Ubuntu verification (owner-run)

First run the prerequisites only (no coordinator is started), then the self-test, from a clean checkout at the pushed commit:

```bash
bash scripts/stage03-ubuntu-check.sh <COMMIT_SHA>
bash scripts/stage03-ubuntu-check.sh <COMMIT_SHA> --run-selftest
```

**Risks the Ubuntu run will settle:**

- The workload now inherits an allow-listed environment and umask 077 (Stage 02 passed the full environment).
- The new device-descriptor rule may flag a DMTCP-internal descriptor below fd 820.
- The single-user check depends on `loginctl` output on the VM.

Each would show as BLOCKED or FAIL with evidence, never as a false PASS.

## 7. Not started

Stage 03b and later stages, daemon integration, multi-user use, production coordinator use, Python or socket-holding workloads, and any CRIU operation.
