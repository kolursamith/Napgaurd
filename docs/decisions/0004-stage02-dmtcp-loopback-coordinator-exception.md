# ADR 0004: Stage 02-Only DMTCP Coordinator Exception via a Pre-Bound Loopback Listener

- Status: **Accepted (limited)** for the same-boot `c-single` diagnostic, which was executed on 2026-10-20 at commit `f0f6599976f5a7fdeefd8f51413821c5d2606516` with the user's approval. **Amendment 1 (cross-reboot) is Proposed**; it is not to be run until the user explicitly approves the first cross-reboot test.
- Amended: 2026-10-20 (scope split; Amendment 1)
- Date: 2026-10-19
- Scope: **Stage 02 feasibility experiments only.** It does not apply to NapGuard production (Stages 03–12).
- Related: [Stage 02 report](../phases/STAGE-02-report.md) (I-1, I-1a, I-1b, I-1c, section N), [backend contract](../architecture/checkpoint-backend.md), [IPC and security](../architecture/ipc-security.md)
- Requirements: SEC-001, SEC-002, SEC-010, CMP-001, PLT-009

## Scope

| Scope | Status | What it covers |
|---|---|---|
| **Approved** | executed once; **VERIFIED PASS** (Stage 02 report section T) | `trial --backend dmtcp --class c-single --adr-0004-controlled-trial`: a same-boot checkpoint/restore diagnostic under every condition below |
| **Proposed (Amendment 1)** | implemented; **not yet run**, needs explicit approval for the first run | `reboot-prepare` and `reboot-resume` with `--backend dmtcp --class c-single --adr-0004-controlled-trial`, with the extra conditions in "Amendment 1" |
| **Prohibited** | refused by the harness or by this ADR | production use (any NapGuard daemon, CLI, or GUI outside `experiments/stage02`); TCP or any socket-holding workload; multi-user hosts or use by anyone other than the single VM user; every other workload class (`c-threads`, `py`, `c-file-*`, `c-child`, `tcp`, `gui`); `matrix`; CRIU under this exception; any DMTCP version other than 4.2.0 |

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

## Amendment 1: cross-reboot `c-single` (proposed)

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

This ADR expires when Stage 02 ends. It must be reviewed immediately if any of these happens:

- DMTCP is upgraded;
- an audit reports exposure;
- a workload class other than `c-single` is proposed;
- a cross-reboot DMTCP trial is proposed, which needs an amendment.
