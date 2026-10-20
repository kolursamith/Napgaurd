# Stage 03 Plan: Backend Integration

- Status: **Plan only. Implementation has not started.**
- Date: 2026-10-20
- Entry gate:
  - **Not yet satisfied.** Every Stage 02 trial has been executed, and Stage 02 is ready for the owner's closure decision ([Stage 02 report section AB](STAGE-02-report.md#ab-final-stage-02-evidence-compatibility-matrix-and-closure-readiness)):
    - VERIFIED PASS: `c-single` (same boot and cross-reboot), `c-threads`, `c-file-static`, `c-file-grow`;
    - VERIFIED FAIL: `py`;
    - BLOCKED: `c-child`, `tcp`, `gui`.
  - Implementation starts only after Stage 02 is closed, ADR 0005 is accepted, [ADR 0006](../decisions/0006-stage03-dmtcp-backend-integration-and-coordinator-security.md) (drafted, Proposed) is approved, and this plan is approved.
- Roadmap definition ([traceability](../requirements/traceability.md#approved-stage-roadmap)): **Stage 03 = backend integration: checkpoint API, adapter, capability detection, CLI.** Gate: *real operations work through NapGuard's API.*
- **Out of scope:**

  | Component | Stage |
  |---|---|
  | Storage and catalog | 04 |
  | Workload registry and supervisor | 05 |
  | Daemon and IPC | 06 |
  | Scheduler | 07 |
  | Power and sleep | 08 |
  | Recovery manager | 09 |
  | GUI | 10 |

## First implementation slice (Stage 03a), in this order

Each step is a separate, reviewable commit on a new branch `stage-03/backend-integration` created from the approved Stage 02 commit. Each step comes with unit tests.

1. **Backend Protocol and data types** (`checkpoint/base.py`): the Stage 01 contract (`probe`, `assess`, `launch`, `status`, `checkpoint`, `restore`, `cancel`, `cleanup`) as a `typing.Protocol`; frozen dataclasses; `ErrorCode` (StrEnum) and `BackendError`. No Linux calls.
2. **Capability detection** (`checkpoint/dmtcp.py: probe`, `checkpoint/criu.py: probe`): find the tools, record absolute paths, the version (`dmtcp_launch --version` only), and tool SHA-256s. `verified=True` only for (4.2.0, aarch64) and the Stage 02-verified reference classes (`c-single`, `c-threads`, `c-file-static`, `c-file-grow`; ADR 0006 §3). CRIU is probe-only.
3. **DMTCP adapter with deny-by-default coordinator policy** (`checkpoint/policy.py`, `checkpoint/dmtcp.py`, `core/proc.py`, `core/netaudit.py`):
   - `CoordinatorPolicy()` refuses by default;
   - `CoordinatorPolicy.experimental_single_user(...)` is required to start the fail-closed loopback hand-off coordinator;
   - ported, reviewed process identity and listener audits.
4. **Checkpoint completion verification** (`checkpoint/dmtcp_completion.py`): port of `ngexp/ckptwait.py`, with the restart script, a stable image, no `.temp`, peers RUNNING, and a deadline.
5. **Restore-state validation**: `restore()` returns a `RestoredSession` and never claims success. The selftest verifies continuity through the API (nonce, rollback window, digests, ballast, progress).
6. **Structured backend errors**: every failure maps to an `ErrorCode`, with `retryable` and a structured `detail` (evidence reason, listings, status, coordinator log lines).
7. **Developer CLI and tests** (`cli.py`, console script `napguard-dev`): `backend probe`, `backend assess`, and `backend selftest --i-accept-single-user-coordinator`. Plus `scripts/stage03-ubuntu-check.sh`.

**Not in Stage 03a:** supervisor or registry (05), SQLite catalog (04), daemon or IPC (06), GUI (10), scheduler (07), power/battery (08), recovery manager (09).

## Requirements delivered in Stage 03

From the traceability matrix:

| ID | Delivered by |
|---|---|
| FR-005, FR-006, FR-034, CMP-003, CMP-006, LIM-004 | `compatibility.py`: assessment, reason codes, promotion to VERIFIED only after a real restore |
| FR-010, FR-011, REL-008 | the DMTCP adapter: checkpoint request, leave-running, a deadline on every call |
| PLT-006, PKG-005 | capability probing: absolute tool paths, versions, guidance when a backend is missing |
| EXC-008 | the contract has no attach operation |
| NFR-004 | everything behind typed interfaces; portable unit tests |
| FR-032 (developer CLI part) | `napguard-dev` |

## Architecture

The modules below are new. The experiment harness in `experiments/stage02/` stays as Stage 02 evidence. The production code is a **reviewed port** of it, not an import from it.

```text
src/napguard/
  core/proc.py            process identity (pid + /proc start ticks + uid + boot id), pidfd
                          signalling, group termination only while the leader is our unreaped child,
                          session-gated adoption, leak scan          (port of ngexp/runner.py)
  core/netaudit.py        ss parsing, loopback-listener creation, wildcard-bind refusal probe,
                          baseline-diff audit, reachability probes   (port of ngexp/runner.py)
  checkpoint/base.py      CheckpointBackend Protocol and data types from the Stage 01 contract;
                          BackendError(code, message, retryable, detail); ErrorCode (StrEnum)
  checkpoint/policy.py    CoordinatorPolicy: the security conditions under which a DMTCP
                          coordinator may run; default = refuse
  checkpoint/dmtcp.py     DmtcpBackend: probe, assess, launch, status, checkpoint, restore,
                          cancel (NOT_SUPPORTED), cleanup
  checkpoint/dmtcp_completion.py   verified-completion wait   (port of ngexp/ckptwait.py)
  checkpoint/criu.py      probe-only CriuBackend: reports BACKEND_UNAVAILABLE or PERMISSION_DENIED
                          (OD-14); no dump or restore in Stage 03
  checkpoint/compatibility.py      CompatibilityReport, reason codes, static rules (CMP-006)
  cli.py                  `napguard-dev` developer CLI (console script in pyproject)
```

### Backend contract (from the [Stage 01 contract](../architecture/checkpoint-backend.md))

- **Interface:** `probe`, `assess(spec)`, `launch(spec, session_dir, deadline)`, `status(session)`, `checkpoint(session, staging_dir, options, deadline)`, `restore(capture, artifact_dir, options, deadline)`, `cancel(handle)`, `cleanup(session, path)`.
- **Data types:** `BackendCapabilities` includes `verified` (true only with recorded Ubuntu evidence for this version and architecture) and `requires_privileges`.

### DMTCP adapter

Behaviour verified in Stage 02:

- **Coordinator:** one per workload, started only through the **fail-closed loopback hand-off**:
  - a pre-bound `127.0.0.1` listener on fd 821, with the same `--coord-port`;
  - the kernel's wildcard-bind refusal probed first;
  - the port file must equal the pre-bound port;
  - post-start `ss` audit plus IPv4 and global-IPv6 connect probes.
- **Launch:** `dmtcp_launch --join-coordinator` with the absolute path from `probe`; stdin from `/dev/null`; logs 0600; the identity recorded.
- **Checkpoint:** a plain `--checkpoint`, then the **verified completion wait**: a restart script naming this port and image, a stable final image, no `.temp` file, peers RUNNING, bounded by the deadline. Exit 0 is never evidence (`--bcheckpoint` does not block in 4.2.0).
- **Restore:** a new hand-off coordinator, then `dmtcp_restart --join-coordinator`. The result is `RestoredSession`; success is decided by the caller's verification (FR-026), never by the restart exiting.
- **Before checkpoint:** the workload must hold no sockets below fd 820 (otherwise the restore opens an `INADDR_ANY` listener, I-1c), and must be a single process until multi-process restore is reviewed.
- **Audits:** listener audits (baseline diff plus ownership) after each step and while waiting. A violation kills the experiment's processes and raises `BackendError(PERMISSION_DENIED, "network exposure")`.
- **Version pin:** the analysed version is 4.2.0. Anything else gives `BACKEND_UNAVAILABLE` with reason `UNANALYSED_VERSION`.

### Capability detection (PLT-006, PLT-009, PKG-005)

- The tools are found under the configured prefix or `PATH`, and absolute paths are recorded.
- The version comes from `dmtcp_launch --version`, which exits before any coordinator code runs.
- The self-check is the version check plus an on-demand isolation check. `verified` is true only for (DMTCP 4.2.0, aarch64) and the Stage 02-verified reference classes (ADR 0006 §3). Everything else is `CANDIDATE` or `UNKNOWN` (CMP-002, CMP-003).
- If DMTCP is missing, the result is `BACKEND_UNAVAILABLE` with build guidance (`build-dmtcp.sh`). NapGuard never installs it.

### Checkpoint and restore API (used by later stages)

`CheckpointBackend.checkpoint()` returns a `CaptureResult`: the artifact list with roles, the restore hints, and `capture_ms`. Storage (Stage 04) validates, hashes, and publishes those artifacts. `restore()` takes a `CaptureRecord` and the artifact directory.

### Error model

`BackendError.code` takes one of these values:

- `BACKEND_UNAVAILABLE`, `UNSUPPORTED_WORKLOAD`, `PERMISSION_DENIED`, `LAUNCH_FAILED`
- `CHECKPOINT_FAILED`, `RESTORE_FAILED`, `RESOURCE_CONFLICT`
- `VERSION_MISMATCH`, `ARCH_MISMATCH`, `TIMED_OUT`, `CANCELLED`, `INTERNAL`

Each error also carries `retryable` and a structured `detail`. For checkpoints, `detail` includes the evidence reason, the directory listing, the status fields, and the coordinator log lines. Error codes map onto the IPC codes in [IPC and security](../architecture/ipc-security.md#error-codes).

### Developer CLI (minimal)

```text
napguard-dev backend probe [--json]
napguard-dev backend assess -- <executable> [args...]
napguard-dev backend selftest --i-accept-single-user-coordinator
    # launch ngcounter c-single → checkpoint → terminate → restore through the API
    # → verify continuity, then print evidence and a PASS/FAIL/BLOCKED verdict
```

The `--i-accept-single-user-coordinator` flag maps to `CoordinatorPolicy.experimental_single_user`. Without it, every coordinator start is refused (fail-closed by default).

## Security conditions carried forward

- **ADR 0004 is Stage 02-only.** Stage 03 needs **ADR 0006** before any DMTCP coordinator runs through `src/napguard`. ADR 0006 would carry ADR 0004's conditions into the adapter's `CoordinatorPolicy`:
  - DMTCP 4.2.0;
  - single process;
  - no workload sockets;
  - the loopback hand-off;
  - audits;
  - an explicit developer flag;
  - single-user VM only.
- **Still not production-approved.** Local users can command a loopback coordinator because there is no authentication (SEC-002). That must be solved before Stage 06 exposes it through the daemon. Options to evaluate: per-workload network isolation through an approved mechanism, an authenticated relay, or an upstream DMTCP change.
- **No privilege changes:** no sudo, capabilities, sysctl, AppArmor, or firewall changes. CRIU stays probe-only (OD-14).

## Tests and Ubuntu validation

| Level | Tests |
|---|---|
| Unit (any host) | contract conformance with a fake backend; error-code mapping; capability parsing; version pin; policy refusal by default; ELF static and setuid detection; completion-wait rules (ported virtual-clock tests); `ss` parsing and baseline diff |
| Integration (Linux, fake tools) | fake `dmtcp_command` and coordinator: delayed completion, never-completing, workload death, foreign image, exposure abort |
| Linux system (Ubuntu, real DMTCP) | `napguard-dev backend selftest`: c-single checkpoint and restore through the API with continuity verification; refusal cases (wrong version, socket-holding workload, multi-process workload) |
| Failure | coordinator killed mid-checkpoint; image removed before restore; audit tool missing |

Ubuntu validation would add `scripts/stage03-ubuntu-check.sh <SHA>`, modelled on `ubuntu-controlled-trial.sh`:

1. Check the SHA and a clean tree.
2. Run the baseline.
3. Run the Linux tests with zero skipped.
4. Take a listener snapshot.
5. Run `napguard-dev backend probe`.
6. Run `napguard-dev backend selftest`.
7. Check for new listeners and leftover processes.
8. Bundle the evidence.

## Gate (proposed)

| Criterion | Evidence |
|---|---|
| The DMTCP c-single checkpoint and restore works **through NapGuard's API** on Ubuntu ARM64 | `selftest` PASS log |
| Capability report correct with DMTCP present, absent, and at an unanalysed version | system test logs |
| Fail-closed by default (no flag means no coordinator) | unit and system tests |
| No network exposure, no leftover processes | listener diff, leak scan |
| pytest, Ruff, and mypy pass on Ubuntu | baseline log |

## Open decisions before implementation

1. Approve the Stage 02 closure decision ([section AB.5](STAGE-02-report.md#ab5-closure-readiness-verdict)).
2. **ADR 0006**: the DMTCP coordinator policy for Stage 03 developer use. Drafted as [ADR 0006](../decisions/0006-stage03-dmtcp-backend-integration-and-coordinator-security.md). It includes the CMP-006 rules from Stage 02 (`KNOWN_RESTORE_FAILURE` for Python, `SOCKETS_HELD`, `MULTI_PROCESS`, `DEVICE_STATE`).
3. **The backend-decision ADR (OD-01)**: drafted as [ADR 0005](../decisions/0005-checkpoint-backend-selection.md) (Proposed). DMTCP is primary and provisional; CRIU is deferred.
4. Placement of the shared process and network-audit utilities (`napguard.core`, proposed).
