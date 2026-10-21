# Stage 03a Report: DMTCP Backend Integration (Developer Mode)

- Date: 2026-10-21
- Branch: `stage-03/backend-integration`, from the Stage 02 closure commit `b7fcddfe582e112cbc69f5ce4078629dd2e06041`
- Authorization: [ADR 0006](../decisions/0006-stage03-dmtcp-backend-integration-and-coordinator-security.md), accepted **only** for Stage 03a developer mode on the isolated single-user Ubuntu 24.04 ARM64 VM
- Status: **AWAITING UBUNTU VERIFICATION.** Mac validation passes. The Stage 03a gate (a genuine checkpoint/restore through NapGuard's API) is **NOT RUN**. Stage 03a is not passed until `scripts/stage03-ubuntu-check.sh <SHA> --run-selftest` reports PASS on Ubuntu.

## 0. Standing facts

- **Stage 02 is CLOSED** (PASS with recorded limitations; [Stage 02 report section AC](STAGE-02-report.md#ac-owner-closure-decision-2026-10-20)). ADR 0004 and all its amendments have **expired**. Stage 03a does not use them: `src/napguard` never imports the Stage 02 harness, and its coordinator use is authorized only by ADR 0006.
- **Stage 03a is developer-only.** A coordinator starts only with `--i-accept-single-user-coordinator`, for that invocation only. The flag is never stored. The default policy refuses.
- **The DMTCP coordinator is unauthenticated.** Loopback confinement keeps remote hosts out. The single-user check reduces, but does not remove, the risk from other local users. **This is not a production security solution** and it blocks Stage 06.
- **Python restoration failed in Stage 02** (2 of 2 restores; SIGSEGV). Python workloads are refused (`KNOWN_RESTORE_FAILURE`).
- **External filesystem rollback is not guaranteed** (LIM-007). Every manifest states this.
- `DMTCP_LOOPBACK_ONLY_COORDINATOR_VERIFIED` remains `False`. Stage 03b and every later stage remain **unauthorized**.

## 1. Implemented components

| Module | Purpose |
|---|---|
| `src/napguard/core/proc.py` | Process identity (PID + start ticks + UID + boot ID); pidfd signals; group termination only while the leader is our unreaped child; session-gated adoption; allow-listed environment (SEC-008); umask 077; private logs; crash-cleanup state file; report-only leak scan |
| `src/napguard/core/netaudit.py` | `ss`/`ip` audits, `ListenerGuard` (baseline diff plus ownership), loopback listener, wildcard-bind refusal; every audit fails closed (`AuditError`) |
| `src/napguard/checkpoint/base.py` | Backend contract: `CheckpointBackend` Protocol, typed results, stable `ErrorCode`s, `BackendError`, deadlines |
| `src/napguard/checkpoint/policy.py` | `CoordinatorPolicy`: deny by default; `experimental_single_user` needs the per-invocation flag and a single-user host |
| `src/napguard/checkpoint/compatibility.py` | Reason codes with messages (FR-034); static and runtime fail-closed rules (CMP-006); levels (CMP-002, CMP-003) |
| `src/napguard/checkpoint/artifacts.py` | Artifact validation (flat names, private directory, `O_NOFOLLOW`, owner and permission checks, SHA-256 from the opened descriptor, `CORRUPT`); filesystem type; directory fsync |
| `src/napguard/checkpoint/manifest.py` | Strict checkpoint manifest: schema, digest, artifact list, backend version, tool hashes, architecture, kernel, and the mandatory limitation statements (LIM-007, LIM-002, LIM-003) |
| `src/napguard/checkpoint/dmtcp_completion.py` | Verified completion wait (code identical to the Stage 02 `ckptwait.py`) |
| `src/napguard/checkpoint/dmtcp.py` | `DmtcpBackend`: probe (version **and** pinned source commit), assess, launch, status, checkpoint, stop, restore, adopt, audit, diagnostics, cleanup |
| `src/napguard/checkpoint/criu.py` | CRIU **probe only** (`--version`, `getcap`); every operation is refused (OD-14); no `criu check`, no sudo |
| `src/napguard/selftest/` | Reference workload `ngcounter.c` (byte-identical to Stage 02), state model (Stage 02 rules; the file reader now reads only complete lines, see section 11), continuity verifier, self-test runner |
| `src/napguard/cli.py` | `napguard-dev backend probe`, `assess`, `selftest --i-accept-single-user-coordinator`, `cleanup` (`backend …` aliases) |
| `scripts/stage03-ubuntu-check.sh` | Ubuntu gate: prerequisites by default; one self-test only with `--run-selftest` |

## 2. Architecture decisions

| Decision | Record |
|---|---|
| **Synchronous calls with explicit deadlines.** Stage 06 runs them in worker threads (`asyncio.to_thread`); the GUI and CLI reach the backend only through the daemon's IPC, so no event loop is blocked | [ADR 0007](../decisions/0007-synchronous-backend-calls-in-stage-03a.md), **Proposed** (needs owner approval) |
| **Images are not relocated.** Stage 02 verified restore only from the directory DMTCP wrote the images to. `checkpoint()` requires `staging_dir` to be the session's DMTCP checkpoint directory, and the restore reads the images there. The manifest is written next to them. Relocation stays unverified, and is left to Stage 04 storage design with its own Ubuntu evidence | this report; [backend contract](../architecture/checkpoint-backend.md) note |
| **Never execute the DMTCP-generated restart script.** It is parsed only as completion evidence; restores use NapGuard-built argument vectors | ADR 0006 §4 |
| **Pinned engine.** Probe and assessment are `VERIFIED` only for DMTCP 4.2.0 built from commit `f8009ce7b4ad211311ca2f72a929b975e4aa1155` (`napguard-build-info.txt` from `build-dmtcp.sh`) on Ubuntu 24.04 aarch64 | ADR 0005, ADR 0006 |
| **No attach.** Only workloads NapGuard launches can be checkpointed | EXC-008 |

## 3. Security boundaries and how each is enforced

- **Coordinator start, in this order:**
  1. `CoordinatorPolicy.authorize`, before anything is bound or spawned;
  2. DMTCP version 4.2.0;
  3. a listener baseline is taken;
  4. a 127.0.0.1 listener is created, and the kernel must refuse a wildcard bind on its port;
  5. the listener is handed over on fd 821 with the same `--coord-port`;
  6. the port file must match;
  7. `ss` must show loopback only, and connects to every non-loopback IPv4 and global IPv6 address must fail.

  Any failure, or an audit that cannot run, kills the coordinator and closes the listener.
- **Continuous audit:** after launch, before and after the checkpoint, on every completion poll, after the restore, and on every self-test wait and verification-window poll. Exposure kills the session and makes the run **FAIL**. A failed audit can never yield PASS.
- **Process ownership:**
  - every process starts from an argument array, never a shell, with stdin from `/dev/null`, a closed descriptor set, an allow-listed environment, umask 077, and its own session;
  - signals go through pidfds after the identity is re-checked;
  - groups are signalled only while their leader is our unreaped child;
  - a restored process is adopted only if it belongs to this user and to this restore's session, and started after the restore began;
  - crash cleanup (`napguard-dev cleanup`) skips records from other boots, other users, or whose identity changed;
  - the leak scan **reports and never kills**.
- **Artifacts:** private 0700 session directory, single-component names, `O_NOFOLLOW`, regular files only, owner-only permissions, SHA-256 computed from the opened descriptor, and an fsync of the directory. Before restore:
  - the manifest must validate strictly;
  - every artifact is re-hashed (`CORRUPT` on change);
  - backend version, tool hashes, and architecture must match.

  Path traversal, symlinks, permission changes, and tampering are refused before any process starts.
- **Failure handling:**
  - a failed or timed-out checkpoint leaves the workload running and removes only temporary files;
  - a failed restore records the restarter's exit code or signal (read without reaping it) and unfiltered logs, then stops everything;
  - cleanup is idempotent.

## 4. Workloads

| Workload | Stage 03a status |
|---|---|
| NapGuard reference `c-single`, `c-threads` | `VERIFIED` on the pinned platform; covered by the self-test |
| NapGuard reference `c-file-static`, `c-file-grow` | `VERIFIED` on the pinned platform (Stage 02 evidence); no self-test; LIM-007 applies |
| Other single-process, dynamically linked native programs | `UNKNOWN`; never advertised as supported |
| Python (`KNOWN_RESTORE_FAILURE`), socket-holding (`SOCKETS_HELD`), multi-process (`MULTI_PROCESS`), GUI (`GUI_TOOLKIT`, display sockets), devices (`DEVICE_STATE`), static (`STATIC_BINARY`), setuid/setgid (`SETUID`), wrong architecture (`ARCH_MISMATCH`), other DMTCP versions (`UNANALYSED_VERSION`) | `UNSUPPORTED`, refused before launch and again before every checkpoint |

## 5. Stage 02 mechanisms reused (reviewed ports)

- The fd-821 loopback hand-off and the isolation audit.
- The verified completion wait (code identical).
- Process identity, adoption, and group termination.
- The reference workload (byte-identical) and its state model (identical digest, ballast, and parsing rules; only complete, newline-terminated lines are read).
- The continuity rules: same nonce; first restored counter in `[ckpt_lower, kill)`; digests; ballast; progress; original confirmed dead. The port adds a refusal of checkpoints at counter 0.
- Non-reaping restarter diagnostics.

## 6. Mac validation (macOS arm64; no DMTCP)

| Check | Result |
|---|---|
| `pytest` (whole repository) | **323 passed, 30 skipped, 0 failed.** All skips are Linux-only: 19 from Stage 02, 11 from Stage 03 |
| Stage 03 tests (`tests/unit/test_stage03_*.py`, 7 modules) | 122 tests: 111 passed, 11 skipped (Linux-only) |
| `ruff check .` | pass |
| `mypy` (strict, Linux platform) | pass |
| `bash -n` on every script | pass |
| `git diff --check` | clean |
| Documentation consistency (`tests/docs`) | pass |

**Coverage.** Mocked and fake-backend tests are labelled. Linux-only tests use real processes, `ss`, and `ip` with FAKE coordinators, never DMTCP.

| Area | Where |
|---|---|
| Backend discovery, version, pinned source commit | `test_stage03_dmtcp.py`: missing tools, version mismatch, verified platform, unpinned commit |
| CRIU probe only | `test_probe_without_dmtcp_is_blocked` (probe reports CRIU unavailable); `criu.py` refuses every operation |
| Default deny; missing acknowledgement; multi-user host | `test_stage03_policy_artifacts.py`, `test_stage03_dmtcp.py`, `test_stage03_cli.py` |
| Fake coordinator attempting a wildcard bind; exposed listener | `test_coordinator_ignoring_the_handoff_fails_closed`, `test_exposed_coordinator_is_killed` (Linux) |
| IPv4/IPv6 listener audits; failed reachability; audit tool failure | `test_isolation_failures_are_refused` (5 cases), `test_audit_tool_failure_during_isolation_is_refused`, `test_listener_baseline_failure_refuses`, `test_missing_audit_tool_is_an_error` |
| Process identity mismatch, PID reuse | `test_forged_identity_and_reused_pid_are_never_signalled`, `test_crash_cleanup_stops_only_identity_matched_processes`, `test_cleanup_stops_only_recorded_identities` (Linux) |
| Symlinks, path traversal, permissions, hash mismatch | `test_stage03_policy_artifacts.py`, `test_restore_refuses_tampered_or_incompatible_captures`, manifest traversal cases |
| Invalid or incomplete manifest | `test_invalid_or_incomplete_manifests_are_refused` (12 cases), `test_missing_garbled_symlinked_or_exposed_manifest_is_refused` |
| Completion timeout; incomplete image; process exit during checkpoint | `test_checkpoint_completion_timeout_is_reported`, `test_incomplete_checkpoints_are_never_complete` (6 cases), `test_checkpoint_timeout_leaves_workload_running_then_cleanup` (Linux) |
| Checkpoint failure preserving the workload; restore failure and cleanup | `test_checkpoint_timeout_leaves_workload_running_then_cleanup`, `test_failed_restore_reports_diagnostics_and_cleans_up` (Linux), `test_failed_checkpoint_restore_or_manifest_is_fail_and_cleans_up` |
| Unsupported workloads | static and runtime rule tests, GUI toolkits, `test_unsupported_workload_is_refused_before_launch` |
| Reference nonce/counter verification; fresh-start detection; absent restored reports; restore command alone | `test_stage03_selftest.py` (fake backend): `test_genuine_restore_passes_every_check`, `test_no_false_pass` (new process, no rollback, surviving original, foreign process, early death, silent restore, tmpfs), `test_verifier_requires_rollback_window_and_progress` |
| Safe repeated cleanup | `test_cleanup_is_safe_to_repeat`, the repeat in the Linux checkpoint-timeout test, and the Ubuntu script's cleanup step |
| Ubuntu script strictness | `test_ubuntu_script_arguments_are_strict`, `test_ubuntu_script_gates_and_defaults` |

## 7. Ubuntu verification procedure and evidence

Run on the VM, from a clean checkout at the pushed commit, as the ordinary user.

1. **Prerequisites only.** This starts no coordinator. Checks:
   - exact SHA and a clean tree;
   - Ubuntu 24.04 on aarch64;
   - Stage 00 baseline;
   - Stage 03 Linux tests with zero skips;
   - probe: DMTCP 4.2.0 from the pinned commit, on the verified platform;
   - the refusal without the flag;
   - listener diff and leftover processes.

   ```bash
   bash scripts/stage03-ubuntu-check.sh <COMMIT_SHA>
   ```
2. **One real self-test.** It adds the developer-mode self-test, then `napguard-dev cleanup` on both session directories.

   ```bash
   bash scripts/stage03-ubuntu-check.sh <COMMIT_SHA> --run-selftest
   ```

**Evidence:**

| Path | Content |
|---|---|
| `~/napguard-stage03-evidence-<UTC>/` | `verdict.txt`, `steps.txt`, `host.txt`, `baseline.log`, `stage03-tests.log`, `probe.txt` and `probe.json`, `policy-refusal.log`, `selftest.log`, `cleanup-session-*.json`, listener snapshots, `leftover-processes.txt`, `selftest-run.tgz` (logs, reports, manifest, `selftest-result.json`; **images excluded**) |
| `~/.local/share/napguard-dev/selftest/<UTC>-<class>-<hex>/` | the run itself: `selftest-result.json`, `reports/managed.txt`, `session-launch/` (logs, `session.json`, `dmtcp-ckpt/` with the image(s), restart script, and `napguard-manifest.json`), `session-restore/` |

## 8. Stage 03a acceptance criteria

All of these must hold on Ubuntu, from one `--run-selftest` run:

1. `verdict.txt` says `STAGE 03A UBUNTU VERIFICATION: PASS`.
2. `stage03-tests.log` passes with no skips.
3. `probe.txt` shows `available=True`, `verified=True`, and `source_commit=f8009ce7b4ad211311ca2f72a929b975e4aa1155`.
4. `policy-refusal.log` shows the refusal and that no coordinator appeared.
5. `selftest-result.json` has `status: PASS`, with every check PASS:
   - backend available; reference workload VERIFIED; managed workload running;
   - checkpoint verified complete; artifacts on a persistent filesystem; workload continued after checkpoint;
   - original terminated; manifest validated; restore started;
   - restored process owned by this restore; restored process stayed alive;
   - restored state continuity for every role; processes cleaned up.
6. `nonloop-new.txt` and `leftover-processes.txt` are empty, and both `cleanup-*.json` files show `"stopped": []`.

## 9. Known limitations

1. The self-test covers `c-single` and `c-threads` only.
2. Images are not relocated; publication is Stage 04.
3. The single-user check is a heuristic. It ignores system accounts (UID < 1000) and users who appear after the check.
4. Whether a DMTCP worker survives the loss of its coordinator is untested.
5. Since Stage 02 the workload inherits an allow-listed environment and umask 077, and runs under a new device-descriptor rule. These are unverified with real DMTCP until the Ubuntu run; problems would show as FAIL or BLOCKED.
6. `CANDIDATE` is not used: anything without evidence is `UNKNOWN`.

## 10. First Ubuntu self-test: network-audit failure during restore (commit `e81c0102`)

**Observed** (user-reported):

- Run `~/.local/share/napguard-dev/selftest/20261021T053011Z-c-single-75b9`.
- These steps passed: checkpoint verified; artifacts on a persistent filesystem; original terminated; manifest validated; restore started.
- The run then **failed** with `PERMISSION_DENIED: network exposure after restore`.
- The listener was UDP, `0.0.0.0:50304`, with no visible owning PID (`[]`).
- No new non-loopback listener remained after cleanup.
- **Restored-state continuity was not verified.** This run is a FAIL, and it stays recorded as one.

**Analysis:**

1. **The rule.** Since Stage 02, the guard's second rule counted **every** new non-loopback listener of **any** owner as exposure.
2. **UDP sockets are listed even when nobody listens.** UDP has no listening state, so `ss -l -u` lists every unconnected UDP socket, including a system service's short-lived client socket on `0.0.0.0:<ephemeral>` (DNS, time sync, mDNS).
3. **The socket was not ours.** `ss -p` names owners only for processes whose descriptors this user can read, that is, our own. A socket with no visible owner therefore belongs to another account.
4. **It does not fit DMTCP.** DMTCP's documented restore-time exposure is a **TCP** listener for images that contain sockets (I-1c). The workload holds none; the precheck verifies this.
5. **Not proven.** The evidence did not record the socket's owner, so it is not proven that the socket was a host socket.

**Change (narrow):**

- `ss` now runs with `-e`. Each socket's kernel owner UID and inode are parsed from the end of the line, so a process name cannot spoof them.
- A new non-loopback socket is set aside only if it is provably owned by root or a system account and no process of ours is attached. It is recorded in `host_sockets_seen` with protocol, address, UID, inode, and step.
- **Still FAIL:** new listeners of this user, of another regular user, or of unknown owner; any listener of a session process.
- **Unchanged:** the coordinator isolation checks (loopback-only TCP listener on the handed-over port, wildcard-bind refusal, IPv4/IPv6 reachability).
- Exposure errors now also record the observation time, the session's PIDs and process identities, and the host sockets seen.
- Regression tests: `test_ss_details_give_the_kernel_owner_and_resist_spoofed_names`, `test_new_system_account_udp_socket_is_noted_not_an_exposure`, `test_other_new_listeners_remain_exposures` (6 cases), `test_session_owned_wildcard_listener_is_always_an_exposure`, `test_host_socket_notes_are_bounded`, `test_exposure_detail_names_owner_inode_and_session`.
- This clarifies ADR 0006 §1.4. **Owner review requested.**

**If the same socket appears again** and is owned by this user, or by an unknown owner, the self-test still FAILs. The new evidence (owner UID, inode, session PIDs) will then identify it.

## 11. Report-reading race in the Linux test run (commit `5042e1e5`)

**Observed** (user-reported). On Ubuntu, the Stage 00 baseline passed, but the Stage 03 Linux tests gave **133 passed, 1 failed**:

- `test_genuine_restore_passes_every_check` raised `MalformedReport` on a half-written line ending in `counter=`;
- the real DMTCP self-test was not run.

**Cause.** The self-test polls the report file while the workload appends to it. `read_reports()` and `count_lines()` used `splitlines()`, which also returns an unfinished last line:

- the strict parser rejected that line;
- a mark taken mid-write could make a later read start after the line and skip that record.

The fake workload made this likely because it appends several lines through a buffered file object. The real C workload writes each line with one `write()`, but a concurrent reader can still in principle see a partial line. The old reader, run on the exact fragment, raises the reported error.

**Fix:**

- Only newline-terminated lines are read and counted (`model.complete_lines`). A trailing fragment is left for a later poll, so it is neither an error nor skipped.
- Complete records are parsed exactly as strictly as before.
- A malformed **complete** record now ends the self-test as a recorded FAIL ("reports well-formed"), with cleanup.

**Tests:**

- `test_a_partial_trailing_line_is_left_for_a_later_poll`;
- `test_malformed_finished_records_are_still_rejected` (6 cases);
- `test_split_writes_do_not_break_a_genuine_restore` (every line written in two halves);
- `test_a_corrupt_finished_report_is_a_failure_with_cleanup`.

No coordinator or network-audit rule changed.

## 12. Not started

Stage 03b and later stages, daemon integration, multi-user use, production coordinator use, Python or socket-holding workloads, and any CRIU operation.
