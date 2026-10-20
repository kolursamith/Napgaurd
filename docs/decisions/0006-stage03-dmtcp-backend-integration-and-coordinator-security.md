# ADR 0006: Stage 03 DMTCP Backend Integration and Coordinator Security (Developer Use Only)

- Status: **Proposed. Owner approval requested.** No code exists for it yet. Stage 03 implementation starts only after Stage 02 is formally closed, [ADR 0005](0005-checkpoint-backend-selection.md) is accepted, and this ADR is approved.
- Date: 2026-10-20
- Scope: **Stage 03 (backend integration) developer use** through `src/napguard` on the single-user Ubuntu 24.04 aarch64 UTM VM. **It does not authorize production use**, the daemon (Stage 06), or any network- or IPC-reachable checkpoint function.
- Supersedes: nothing. [ADR 0004](0004-stage02-dmtcp-loopback-coordinator-exception.md) (Stage 02 experiments only) expires at Stage 02 closure. This ADR carries its technical conditions forward but **not its authorization**.
- Related: [Stage 02 report section AB](../phases/STAGE-02-report.md#ab-final-stage-02-evidence-compatibility-matrix-and-closure-readiness), [Stage 03 plan](../phases/STAGE-03-plan.md), [backend contract](../architecture/checkpoint-backend.md), [IPC and security](../architecture/ipc-security.md)
- Requirements: SEC-002, SEC-003, SEC-004, SEC-006, SEC-010, SEC-011, REL-006, REL-008, CMP-002, CMP-003, CMP-004, CMP-005, CMP-006, FR-005, FR-006, FR-026, FR-034, LIM-003, LIM-004, LIM-007, PLT-006, PLT-009, EXC-001, EXC-008

## Context

**Stage 02 evidence** (user-reported, DMTCP 4.2.0, Ubuntu 24.04.5 aarch64):

- `c-single` passed same-boot and cross-reboot.
- `c-threads`, `c-file-static`, and `c-file-grow` passed same-boot. `c-file-grow` showed LIM-007: writes after restore were appended after the pre-termination data.
- `py` is a **verified failure**: 2 of 2 restores failed, the second with SIGSEGV. The root cause is unknown.
- `c-child`, `tcp`, and `gui` were blocked by policy.

**The DMTCP 4.2.0 coordinator is the security problem:**

1. It binds `INADDR_ANY` by default and has **no authentication**. Any process that can reach its port can command it: kill, checkpoint, status.
2. The only loopback-only method found is DMTCP's internal fd-821 hand-off (`PROTECTED_COORD_FD`). It is undocumented, pinned to 4.2.0, and made fail-closed by NapGuard's own checks.
3. A restored image that holds workload sockets makes `dmtcp_restart` open an `INADDR_ANY` restore listener (I-1c).
4. Loopback confinement stops remote access only. **Every local user** can still reach `127.0.0.1:<port>`. This conflicts with SEC-002's rule that only the owning UID may command NapGuard.

**Why Stage 02 is not production authorization.** ADR 0004 authorized a fixed number of owner-run experiments in `experiments/stage02` on one single-user VM. It names exact classes and trial counts, and it expires when Stage 02 ends. Its evidence shows that a carefully constrained coordinator was not network-exposed **during those runs**. It does not show:

- that local users are excluded (they are not);
- that the hand-off is stable across DMTCP versions;
- that arbitrary workloads behave like the reference workloads (`py` shows they may not).

The harness constants `DMTCP_LOOPBACK_ONLY_COORDINATOR_VERIFIED = False` and `CONTROLLED_TRIAL_CLASSES = ("c-single",)` **stay unchanged**. This ADR does not touch them, and `src/napguard` does not import them.

## Decision (proposed)

### 1. Coordinator management

1. **One private coordinator per operation session.**
   - A launch session gets a coordinator that lives as long as the managed workload. Each restore gets a new coordinator.
   - Coordinators are never shared between workloads.
   - The default port (7779) and any default or system coordinator are never used.
   - Every DMTCP tool call passes `--coord-host 127.0.0.1 --coord-port <port> --join-coordinator`, so a tool never starts its own coordinator.
2. **Started only through `CoordinatorPolicy`.** `checkpoint/policy.py` holds the conditions under which a coordinator may start.
   - Its default instance **refuses**.
   - The only permissive constructor is `CoordinatorPolicy.experimental_single_user(...)`. It is reachable only from the developer CLI flag `--i-accept-single-user-coordinator`, given **per invocation**.
   - There is **no production mode** in Stage 03.
3. **Fail-closed loopback hand-off** (ported from Stage 02 and re-tested):
   - DMTCP must report exactly 4.2.0;
   - the kernel must refuse a wildcard bind on the chosen port;
   - a pre-bound `127.0.0.1` listener is passed on fd 821, together with the same `--coord-port`;
   - the coordinator's port file must equal the pre-bound port;
   - `ss` must show only loopback listeners for the coordinator;
   - connects to every non-loopback IPv4 address and every global IPv6 address must fail.

   Any failure, or an audit that cannot run (for example, `ss` missing), kills the coordinator and fails the operation.
4. **Continuous audit.** Listener audits run after launch, after checkpoint, every ~0.5 s while waiting for a restore, and after restore. They compare against a baseline and check ownership. A violation kills every process of the session and raises `BackendError(PERMISSION_DENIED, "network exposure")`.
5. **Lifetime and files.** The coordinator's port file and logs live in the session directory (mode 0700, files 0600). The coordinator stops when its session ends; stopping is verified by process identity.
   - It is **UNVERIFIED** whether a DMTCP worker survives the loss of its coordinator. Stage 03 must test this before any code relies on either answer.

### 2. Preventing exposure of unauthenticated coordinator control

| Threat | Stage 03 control | Residual risk |
|---|---|---|
| Remote host commands the coordinator | loopback-only hand-off; wildcard-bind refusal; IPv4/IPv6 reachability probes; continuous audits (§1) | none known within these checks; the hand-off is undocumented and pinned to 4.2.0 |
| A restored image opens an `INADDR_ANY` listener (I-1c) | refuse to checkpoint workloads that hold sockets (§5); audits during restore | a restore of an image that gained a socket after the precheck is caught by the audit within ~0.5 s and aborted |
| **Another local user** commands the coordinator (SEC-002) | single-user host check: refuse to start unless no logind session and no process belongs to another UID ≥ 1000; developer flag per invocation; nothing exposed through IPC or the daemon | **not eliminated.** System service accounts (UID < 1000) and users who appear after the check can still connect. Accepted **only** for developer use on the single-user VM. **This is the blocker for production.** |
| Same-UID processes command the coordinator | none beyond SEC-002's trust model (same UID is trusted) | same as any same-user process |
| The coordinator outlives NapGuard | session state file with process identities; `napguard-dev backend cleanup` terminates only identity-matched processes (§4) | a crash between start and record is reported by the leak scan, never killed blindly |

**Before Stage 06 exposes checkpointing through the daemon,** a further ADR must remove the local-user exposure. Candidate approaches:

- (a) a DMTCP change adding a bind address and coordinator authentication (an upstream patch or a pinned fork);
- (b) per-workload network isolation through a mechanism the owner approves (unprivileged user namespaces are blocked by Ubuntu's AppArmor setting, so this needs an approved profile);
- (c) CRIU through a reviewed privilege design (OD-14).

Until then, the daemon must not start DMTCP coordinators.

### 3. Initially supported workload classes (CMP-002, CMP-003, FR-005, FR-006)

Compatibility is assessed per (backend, version, architecture). For Stage 03 that is (DMTCP 4.2.0, Ubuntu 24.04, aarch64).

| Level | Workloads | Behaviour |
|---|---|---|
| `VERIFIED` | the NapGuard reference workloads in the verified classes: single-threaded C, multi-threaded C, unchanged held file, growing held file | allowed. `c-file-grow`-style writers carry the LIM-007 warning |
| `UNKNOWN` | any other single-process, dynamically linked native executable that passes every static and runtime precheck | allowed in the developer CLI only with `--acknowledge-unverified` (CMP-003). It becomes `VERIFIED` only after a real checkpoint and restore of **that** workload (FR-006) |
| `UNSUPPORTED` | every workload matching a rule in §5 | refused with a reason code (FR-034) |

Other platforms stay unverified (PLT-008). No general Python, GUI, TCP, multi-process, or arbitrary-application support is claimed (EXC-001).

### 4. Checkpoint artifact validation, process ownership, cleanup, and recovery

**Capture.** A checkpoint counts only after the **verified completion wait**:

- a restart script naming this session's port and image;
- a stable image (same size and modification time across the settle interval);
- no `.temp` file;
- the expected number of images (= processes = 1);
- peers RUNNING;
- all within the deadline (REL-008).

A command's exit status is never evidence, because `--bcheckpoint` does not block in 4.2.0.

**Artifact checks before handing artifacts on** (SEC-006, SEC-011):

- every artifact is a regular file opened with `O_NOFOLLOW`, owned by the user, mode 0600;
- every artifact is inside the 0700 staging directory; paths are relative and normalized;
- the SHA-256 of every artifact is computed and recorded with the backend name, version, and tool hashes (CMP-004) and the architecture, kernel release, and boot ID (CMP-005).

Stage 04 publishes the artifacts (staging, fsync order, atomic rename).

**NapGuard never executes the DMTCP-generated restart script.** The script is shell code. It is parsed only as completion evidence, and the restore command is built by NapGuard as an argument array (SEC-004).

**Before a restore:**

- re-verify ownership, mode, no symlinks, SHA-256 (REL-006), and the backend version, architecture, and kernel release;
- confirm the original instance is gone (FR-025 is enforced from Stage 05 on).

**Restore result.** `restore()` returns a `RestoredSession` and never claims success.

- A restore counts as successful only when the restored process is alive, owned by the user, adopted by identity, and stays alive through the verification window (FR-026), and the network audit passes.
- The developer `selftest` additionally verifies state continuity: nonce, rollback window, digests, ballast, progress.
- On failure, the record includes the restarter's exit code or signal (read without reaping it), unfiltered log tails, and coordinator diagnostics. The artifacts are kept unchanged (FR-027).

**Process ownership and cleanup** (SEC-003, SEC-004, LIM-006):

- every process starts from an argument array, with `start_new_session`, a closed fd set, an allow-listed environment, and stdin from `/dev/null`;
- identity is PID + `/proc` start ticks + UID + boot ID;
- signals go through a pidfd. Group termination happens only while the leader is NapGuard's own unreaped child;
- a restored process is adopted only if its UID, session, and start time match;
- every operation runs in `try`/`finally`: the session's coordinators and children are stopped, then a leak scan runs. The scan reports any user process that references the session directory and **never kills an unidentified process**.

**Failure recovery:**

- A failed or timed-out checkpoint leaves the workload running (FR-011, REL-008), removes the staging directory, and leaves earlier checkpoints untouched.
- A failed restore stops the restarted tree and the coordinator, and keeps the artifacts.
- If the developer CLI is killed mid-operation, the session state file lets `napguard-dev backend cleanup` stop exactly the recorded, identity-matched processes.
- Durable operation state and startup reconciliation are delivered in Stages 04 and 06.

### 5. Fail-closed behaviour for unsupported workloads and environments (CMP-006, FR-034)

These checks run at assessment, before launch, and again **before every checkpoint**. A refusal leaves a running workload running.

| Condition | Reason code | Source of the rule |
|---|---|---|
| Python interpreter (the executable resolves to `python3.x`, or `libpython` is mapped) | `KNOWN_RESTORE_FAILURE` | Stage 02 `py` verified failure (SIGSEGV), for DMTCP 4.2.0 aarch64 |
| Any workload socket below fd 820 (TCP, UDP, Unix, including display-server connections) | `SOCKETS_HELD` | I-1c `INADDR_ANY` restore listener; LIM-003 |
| More than one process in the managed group | `MULTI_PROCESS` | `c-child` blocked (not reviewed) |
| Open device nodes (for example `/dev/dri`) | `DEVICE_STATE` | LIM-004 |
| Statically linked or setuid/setgid executable | `STATIC_BINARY`, `SETUID` | CMP-006 examples; untested, refused by caution (EXC-002) |
| DMTCP version other than 4.2.0 | `BACKEND_UNAVAILABLE` / `UNANALYSED_VERSION` | the fd-821 analysis is version-specific |
| Not aarch64, or not Ubuntu 24.04 | at most `UNKNOWN`, never `VERIFIED` | PLT-008, CMP-002 |
| Audit tool missing, audit error, or exposure detected | `PERMISSION_DENIED` | §1 |
| Developer flag missing, or the single-user host check fails | `PERMISSION_DENIED` (no coordinator started) | §2 |
| Workload not launched by NapGuard | not addressable | SEC-003, EXC-008 |

### 6. Explicit non-goals

- No production authorization.
- No daemon or IPC path to a coordinator.
- No change to the Stage 02 harness gates.
- No CRIU dump or restore. CRIU stays probe-only (OD-14).
- No sudo, capabilities, sysctl, AppArmor, or firewall changes (SEC-010).
- No DMTCP version change.
- No attach to external processes (EXC-008).

## Consequences

- Stage 03 can deliver its gate, *real operations work through NapGuard's API*, for the verified native classes, without weakening Stage 02's security posture.
- The local-user exposure is explicit and blocks Stage 06. It must be resolved by a later ADR, not by relaxing this one.
- The `py` rule may exclude Python workloads that would in fact restore. That conservative choice is intended. It is revisited if the root cause is found or DMTCP changes.

## Owner approval requested

Approving this ADR authorizes **only** the following:

1. Implementing the Stage 03a slice in the [Stage 03 plan](../phases/STAGE-03-plan.md) on a new branch from the approved Stage 02 closure commit.
2. Starting DMTCP 4.2.0 coordinators from `src/napguard`, **only** through `CoordinatorPolicy.experimental_single_user` and the per-invocation developer flag, under every condition in §1–§5, on the single-user Ubuntu VM.
3. One Ubuntu validation script (`scripts/stage03-ubuntu-check.sh`) modelled on `ubuntu-controlled-trial.sh`, run by the owner.

It does **not** authorize production use, daemon exposure, Python support, multi-process, socket, or GUI workloads, cross-version DMTCP use, or any privilege change.

## Review triggers

- DMTCP is upgraded.
- An audit reports exposure.
- The `py` root cause is found.
- Any proposal to expose checkpointing through IPC or the daemon (Stage 06).
- A proposal to support multi-process or socket-holding workloads.
