# ADR 0004: Stage 02-Only DMTCP Coordinator Exception via a Pre-Bound Loopback Listener

- Status: **EXPIRED** together with all its experimental amendments at Stage 02 closure, by the project owner on 2026-10-20 (closure decision on commit `1890a1ed36b500050c1a4c517cc280ee4a473ff4`). The history below is kept unchanged. Nothing in this ADR authorizes any further trial, or any use of a DMTCP coordinator outside the recorded Stage 02 runs.
- Former status: **Accepted (limited)** for the same-boot `c-single` diagnostic, which was executed on 2026-10-20 at commit `f0f6599976f5a7fdeefd8f51413821c5d2606516` with the user's approval. **Amendment 1 (cross-reboot `c-single`) is Accepted (limited)**: approved by the user and executed once on 2026-10-20 at commit `55905be43fcf67ad1effd2dd5d17e5d1b6c57867`, VERIFIED PASS (Stage 02 report section W). **Amendment 2 (four more classes, same boot) is Approved (limited)** by the project owner on 2026-10-20: exactly one same-boot trial each for `c-threads`, `py`, `c-file-static`, and `c-file-grow`, on the single-user Ubuntu 24.04 ARM64 UTM VM only. **Executed in full** (user-reported): `c-threads` PASS and `py` **FAIL** at `52e5baed59af16b34ccd6ed47cf4037d32e70f07`; `c-file-static` PASS and `c-file-grow` PASS (with the LIM-007 observation) at `9394f850e4c0d1fa1b32715b6b658f0b6526a851`. **Amendment 3 (one `py` diagnostic retest, same boot)** was approved by the project owner on 2026-10-20 as a separate, one-time approval and **executed once** at `9394f850…`: **FAIL** (SIGSEGV). No approval for any further trial remains.
- Amended: 2026-10-20 (scope split; Amendment 1; Amendment 2; Amendment 3)
- Date: 2026-10-19
- Scope: **Stage 02 feasibility experiments only.** It does not apply to NapGuard production (Stages 03–12).
- Related: [Stage 02 report](../phases/STAGE-02-report.md) (I-1, I-1a, I-1b, I-1c, section N), [backend contract](../architecture/checkpoint-backend.md), [IPC and security](../architecture/ipc-security.md)
- Requirements: SEC-001, SEC-002, SEC-010, CMP-001, PLT-009

## Scope

| Scope | Status | What it covers |
|---|---|---|
| **Approved** | executed once; **VERIFIED PASS** (Stage 02 report section T) | `trial --backend dmtcp --class c-single --adr-0004-controlled-trial`: a same-boot checkpoint/restore diagnostic under every condition below |
| **Approved (Amendment 1)** | executed once; **VERIFIED PASS** (Stage 02 report section W) | `reboot-prepare` and `reboot-resume` with `--backend dmtcp --class c-single --adr-0004-controlled-trial`, with the extra conditions in "Amendment 1" |
| **Approved (Amendment 2)** | approved by the project owner on 2026-10-20; **executed**: `c-threads` PASS, `py` **FAIL** (restore), `c-file-static` PASS, `c-file-grow` PASS + LIM-007 observation (Stage 02 report section AB) | exactly one same-boot `trial` each for `c-threads`, `py`, `c-file-static`, `c-file-grow` (see "Amendment 2"); `AMENDMENT_2_APPROVED = True` |
| **Approved (Amendment 3)** | approved by the project owner on 2026-10-20 as a one-time decision; **executed once: FAIL** (SIGSEGV, `CLD_DUMPED`, zero restored reports) | exactly **one** additional same-boot `trial --class py` diagnostic retest; enforced by `backends.ONE_TIME_RETESTS = {"py": 1}` (see "Amendment 3") |
| **Prohibited** | refused by the harness or by this ADR | production use (any NapGuard daemon, CLI, or GUI outside `experiments/stage02`); TCP or any socket-holding workload; multi-user hosts or use by anyone other than the single VM user; `c-child`, `tcp`, and `gui`; **cross-reboot runs of the Amendment 2 classes**; more than one trial per Amendment 2 class without a new approval (for `py`: more than the one Amendment 3 retest); `matrix`; CRIU under this exception; any DMTCP version other than 4.2.0 |

## Context

- **The exposure.** DMTCP 4.2.0's coordinator listens on `INADDR_ANY` and has no authentication. No documented option restricts it, so the Stage 02 harness refuses to start one (I-1a, OD-13).
- **DMTCP's own hand-off.** DMTCP has an internal path: the coordinator first adopts an inherited listener on `PROTECTED_COORD_FD` (fd 821) (`dmtcp_coordinator.cpp:1738-1742`). `dmtcp_launch --new-coordinator` uses the same hand-off internally (`coordinatorapi.cpp:490-508`).
- **Evidence observed so far.** On Ubuntu 24.04 aarch64 at commit `a57149e0bfb42233c1c473c69486cd8a4021c308`, the user ran `dmtcp-isolation-check` against DMTCP 4.2.0 built from the pinned source. They reported **5/5 checks PASS** (exit 0):
  - the coordinator listened on `127.0.0.1:55703` only;
  - a connection to the VM address `192.168.64.3` on that port was refused;
  - `dmtcp_command --status` over loopback succeeded;
  - the coordinator stopped, and all experiment processes were cleaned up.

  The evidence directory is `~/.local/share/napguard-stage02/runs/20261019T142320Z-dmtcp-isolation-check-618c`. The raw files have not been reviewed by Claude.
- **What that evidence does not show.**
  - It does not show checkpointing works. No workload ran.
  - It does not prove fail-closed behaviour against real DMTCP. That property is shown by Linux unit tests with a fake coordinator, plus the kernel's `EADDRINUSE` rule.

## Decision (proposed)

Allow **exactly one controlled experiment shape** to start a DMTCP coordinator, by explicit per-invocation opt-in. Every condition below must hold:

1. **Invocation:** `trial --backend dmtcp --class c-single --adr-0004-controlled-trial`. The flag exists only on `trial`. `matrix`, `reboot-prepare`, `reboot-resume`, and every other class are refused (BLOCKED).
2. **The global gate stays closed.** `DMTCP_LOOPBACK_ONLY_COORDINATOR_VERIFIED` remains `False`, so no default path starts a coordinator.
3. **Fail-closed hand-off for every coordinator** (launch and restore):
   - DMTCP must report version 4.2.0.
   - The harness pre-binds a `127.0.0.1` listener, and the kernel must refuse a wildcard bind on that port.
   - The coordinator receives the listener on fd 821 together with the same `--coord-port`, so a fallback `INADDR_ANY` bind fails.
   - The port file must equal the pre-bound port.
   - `ss` must show only loopback listeners, and connects to every non-loopback IPv4 address must fail.
   - If any of this fails, or the audit itself cannot run, the coordinator is killed and the trial fails.
4. **Workload preconditions before checkpoint:**
   - the managed process group contains exactly one process;
   - the workload holds **no sockets** outside DMTCP's protected descriptors (820–859).

   This keeps the image free of workload sockets, which is the condition under which DMTCP skips its `INADDR_ANY` restore listener (`connectionrewirer.cpp:173-207`, I-1c).
5. **Continuous audit:** listeners are audited after launch and after checkpoint, every ~0.5 s while waiting for the restored workload, and after restore. The trial aborts and kills all experiment processes if either of these appears:
   - (a) a non-loopback listener owned by a known experiment process;
   - (b) **any** non-loopback TCP or UDP listener, whatever its owner, that was not in the baseline taken before the trial started anything.

   Part (b) covers a restored process that is not in the experiment's session. If the baseline cannot be taken, the trial is BLOCKED before anything starts.
6. **Reachability probes** cover every non-loopback IPv4 address and every global-scope IPv6 address of the VM. IPv6 link-local addresses are not probed.
7. **Leak scan:** after cleanup, any live process of the user whose command line or memory mappings reference the run directory makes the trial FAIL. It is reported, not killed.
8. **Version check at the probe:** in controlled mode, `probe()` is BLOCKED unless DMTCP reports 4.2.0.
9. **Verified completion:** a checkpoint counts only after the bounded completion wait (Stage 02 report section S): a restart script naming this run's port and image, a stable final image, no temporary file, and peers RUNNING. A command's exit status is never evidence, because `--bcheckpoint` does not block in DMTCP 4.2.0.
10. **No other change:** no sudo, capabilities, sysctl, AppArmor, or firewall change. No TCP, multi-process, GUI, or reboot experiment.
11. **Procedure:** `experiments/stage02/ubuntu-controlled-trial.sh <SHA>` runs every prerequisite and stops with BLOCKED at the first failure. It runs the trial only when `--run-trial` is given explicitly.

## Amendment 1: cross-reboot `c-single` (accepted, executed)

These are in addition to conditions 1–11 above, which apply to both phases.

1. **`reboot-prepare`:**
   - runs the precheck (one process, no workload sockets);
   - takes the verified checkpoint;
   - terminates and confirms the death of every original process.

   It then cleans up every experiment process and checks that **every coordinator it started has stopped**. Only after that does it write and `sync` `reboot-manifest.json`. The manifest records the boot ID, the host (architecture and hashed machine id), the nonce and marks, the artifact SHA-256s, the workload binary hash, the DMTCP version and tool hashes, the ADR flag, and the Git HEAD. If anything survives, no manifest is written, so there is nothing to resume.
2. **The user powers the VM off and on.** Nothing in NapGuard reboots or powers off the machine.
3. **`reboot-resume`** refuses before any restore (BLOCKED or FAIL) unless all of the following hold:
   - backend, class, and ADR flag match the manifest;
   - this run has not already been resumed successfully;
   - **the boot ID changed**;
   - same machine and architecture;
   - every artifact is present with an unchanged SHA-256, and at least one image exists;
   - the workload binary is unchanged;
   - the DMTCP version and tool hashes are unchanged;
   - a **fresh listener baseline** can be taken in the new boot;
   - a **fresh `dmtcp-isolation-check`** passes in the new boot.

   The restore then uses a new loopback hand-off coordinator, with IPv4 and IPv6 listener audits during and after restore. Success requires the **pre-reboot nonce and a counter inside the pre-reboot checkpoint window**, plus valid digests, an intact ballast, and progress. Cleanup and the leak scan must pass. A passing resume is marked, and a second resume of the same run is refused.

## Amendment 2: four more single-process classes, same boot (APPROVED 2026-10-20, limited)

**Owner decision (2026-10-20):** approved for **exactly one same-boot checkpoint/restore trial per class**: `c-threads`, `py`, `c-file-static`, `c-file-grow`. Only on the isolated single-user Ubuntu 24.04 ARM64 UTM VM. **No production use.** Every ADR 0004 restriction stays mandatory: DMTCP 4.2.0; the fd-821 loopback hand-off; a fresh isolation check before each trial; no workload sockets; single process; IPv4 and IPv6 listener audits; verified checkpoint completion before termination; identity and continuity verification; mandatory cleanup; no sudo, capabilities, firewall changes, or bypasses. `c-child`, `tcp`, `gui`, and cross-reboot runs of these classes remain **prohibited**.

**Scope:** `trial --backend dmtcp --class {c-threads | py | c-file-static | c-file-grow} --adr-0004-controlled-trial`, **same boot only**. One run per class, each started explicitly by the user.

**Conditions:** every ADR 0004 condition (1–11) applies unchanged:

- DMTCP 4.2.0;
- the fail-closed loopback hand-off, with fresh isolation in the same boot;
- precheck: **one process, no workload sockets**;
- verified checkpoint completion (expected images = 1);
- original termination confirmed;
- listener audits (baseline diff plus ownership, IPv4 and IPv6) throughout;
- the leak scan.

**Per class:**

- `c-threads`: continuity must pass for **every** role (`main`, `t0`–`t2`). It exercises multi-thread restore on ARM64, where an aarch64 TODO exists in DMTCP's restart path (B4).
- `py`: continuity and the 1 MiB ballast must pass. The interpreter must still satisfy the no-socket precheck at checkpoint time.
- `c-file-static`: the held file must be **byte-identical** at checkpoint, at termination, and after restore (mandatory).
- `c-file-grow`: the held-file behaviour is **recorded** as a LIM-007 observation (append, rewind, or overwrite). The class verdict is decided by state continuity. A restore failure caused by the changed file is recorded as a finding, not retried.

**Explicitly excluded:** reboot runs of these classes, `c-child`, `tcp`, `gui`, `matrix`, and CRIU.

**Enabling, after approval only.** The gate and the script's strict `--class` allowlist are already implemented and disabled (Stage 02 report section Z). Enabling means one reviewed commit that:

- sets `backends.AMENDMENT_2_APPROVED = True`, which allows the four classes for same-boot `trial` only;
- updates the pinned tests (`test_amendment_2_flag_is_pinned_off`, `test_only_c_single_is_authorised`, `test_amendment_2_classes_are_still_refused`).

## Amendment 3: one `py` diagnostic retest, same boot (APPROVED 2026-10-20, one-time)

**Background.** The Amendment 2 `py` trial at `52e5baed59af16b34ccd6ed47cf4037d32e70f07` created a verified checkpoint (43,099,292 bytes, 1.19 s) and terminated the original. The restore then **failed**: `dmtcp_restart` exited within 1 s. The root cause is unknown. The `setsid()` JWARNING in the restart log is non-fatal in DMTCP 4.2.0 (`dmtcprestartinternal.cpp:323-329`) and also occurs for the passing C classes, so it is not considered the cause (Stage 02 report section AA). The owner's read-only diagnostics found no kernel events in the failure window, about 2.7 GiB available RAM, about 3.8 GiB available swap, overcommit mode 0, and the checkpoint artifacts preserved.

**Owner decision (2026-10-20):** a separate, one-time approval for **exactly one additional same-boot `py` checkpoint/restore diagnostic trial** on the isolated single-user Ubuntu 24.04 ARM64 UTM VM.

**Conditions:** every ADR 0004 condition (1–11) and every Amendment 2 condition stay mandatory, unchanged. In addition:

- **The original failure stays recorded as a FAIL**, even if the retest passes. Its run directory and evidence are preserved, not deleted or overwritten.
- **Exactly one retest, enforced by the harness.** A controlled `py` trial is refused (BLOCKED) unless the run root contains exactly one earlier `py` run that launched a workload: the preserved original. Once the retest has launched, any further `py` trial is refused. A run that was refused or BLOCKED before launching a workload does not count.
- **Diagnostics captured** on any restore outcome: the restarter's exit code or signal (read without reaping it), unfiltered restarter stderr and stdout tails, the restore coordinator log tail, and any workload reports written after termination.
- The Python workload is not modified to force a PASS. DMTCP stays 4.2.0 at its pinned source revision.

**Explicitly excluded:** cross-reboot `py` runs, a second retest, `c-child`, `tcp`, `gui`, `matrix`, CRIU, sudo, capabilities, firewall or kernel tuning, and any change to the production gate (`DMTCP_LOOPBACK_ONLY_COORDINATOR_VERIFIED = False`) or to loopback-only isolation.

**A PASS of the retest would show** that DMTCP 4.2.0 can restore the `py` class on this VM, and would make the original failure intermittent or environmental, which must be reported as such. **A FAIL** is recorded with its diagnostics; any further `py` attempt needs a new approval.

## Consequences

- One same-boot checkpoint, kill, and restore of `c-single` can produce the first real Stage 02 evidence (CMP-001, PRF-001 to PRF-004).
- **This is not production-safe.**
  - Loopback removes network exposure only. Any local user on the VM can connect to `127.0.0.1:<port>` and command the coordinator (kill, checkpoint), because DMTCP has no authentication. This conflicts with SEC-002 and is accepted only for the single-user Stage 02 VM.
  - The approach depends on an undocumented descriptor convention. It is pinned to DMTCP 4.2.0 and must be re-analysed for any upgrade.
- **There is a residual window.** If the "no workload sockets ⇒ no restore listener" reading of DMTCP's source is wrong, a wildcard restore listener could exist until the next audit (at most ~0.5 s plus audit time) before the trial aborts. The exposure is limited to networks that can reach the VM (UTM shared network).

- **False aborts are possible.** An unrelated service that opens a new non-loopback listener during the trial causes a fail-closed abort. That is acceptable.
- **The leak scan has limits.** It relies on the restored process mentioning the run directory in its command line or mappings. DMTCP's restored image is expected to map the workload binary from the run directory, but this is unverified. The procedure also checks for leftover processes with `pgrep`.

## Alternatives rejected

- **Network namespaces:** blocked by Ubuntu 24.04's `apparmor_restrict_unprivileged_userns=1`, and making them work needs root-installed profiles.
- **Firewall rules:** a system change.
- **Disabling AppArmor restrictions:** weakens security globally.
- **A DMTCP patch:** kept as a later option. It would need its own ADR.

## Acceptance criteria for this ADR

1. The user approves this ADR explicitly.
2. A Ubuntu run of the Linux-only Stage 02 safety tests at the commit under review passes. These include the real-kernel wildcard-bind refusal and the fake-coordinator fail-closed test.
3. A fresh `dmtcp-isolation-check` passes in the same boot as the trial.

## Expiry and review triggers

This ADR expires when Stage 02 ends. **Expiry includes every amendment.** After Stage 02 closure, the harness gates remain in the repository as evidence, but the owner's authorization to run further trials under them ends. Stage 03 developer use of a DMTCP coordinator is governed only by [ADR 0006](0006-stage03-dmtcp-backend-integration-and-coordinator-security.md), once approved. **Nothing in this ADR authorizes production use.** It must be reviewed immediately if any of these happens:

- DMTCP is upgraded;
- an audit reports exposure;
- a workload class other than `c-single` is proposed;
- a cross-reboot DMTCP trial is proposed, which needs an amendment.
