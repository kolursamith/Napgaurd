# ADR 0004: Stage 02-Only DMTCP Coordinator Exception via a Pre-Bound Loopback Listener

- Status: **Proposed. Not accepted.** Nothing may run under this ADR until the user explicitly approves it.
- Date: 2026-10-19
- Scope: **Stage 02 feasibility experiments only.** It does not apply to NapGuard production (Stages 03–12).
- Related: [Stage 02 report](../phases/STAGE-02-report.md) (I-1, I-1a, I-1b, I-1c, section N), [backend contract](../architecture/checkpoint-backend.md), [IPC and security](../architecture/ipc-security.md)
- Requirements: SEC-001, SEC-002, SEC-010, CMP-001, PLT-009

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
9. **No other change:** no sudo, capabilities, sysctl, AppArmor, or firewall change. No TCP, multi-process, GUI, or reboot experiment.
10. **Procedure:** `experiments/stage02/ubuntu-controlled-trial.sh <SHA>` runs every prerequisite and stops with BLOCKED at the first failure. It runs the trial only when `--run-trial` is given explicitly.

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
