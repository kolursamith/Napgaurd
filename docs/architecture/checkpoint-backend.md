# Checkpoint Backend Contract

- Status: Stage 01 design. Backend-neutral. **Every statement about DMTCP or CRIU behaviour is UNVERIFIED on Ubuntu 24.04 aarch64 until Stage 02 evidence exists.**
- Related: [components](components.md), [storage and recovery](storage-recovery.md), [requirements](../requirements/requirements.md)

## Purpose

The backend adapter is the only component that talks to a real checkpoint engine. Everything above it (checkpoint manager, storage, recovery, scheduler) works against the contract below. A backend may advertise only the capabilities it has demonstrated in tests (CMP-002). The contract deliberately leaves out "attach to an arbitrary running process" for v1 (EXC-008).

## Interface (design sketch; implemented in Stage 03)

```python
class CheckpointBackend(Protocol):
    name: str  # "dmtcp" | "criu"

    async def probe(self) -> BackendCapabilities: ...
    async def assess(self, spec: WorkloadSpec) -> list[ReasonCode]: ...
    async def launch(self, spec: WorkloadSpec, session_dir: Path,
                     deadline: float) -> BackendSession: ...
    async def status(self, session: BackendSession) -> SessionStatus: ...
    async def checkpoint(self, session: BackendSession, staging_dir: Path,
                         options: CheckpointOptions, deadline: float) -> CaptureResult: ...
    async def restore(self, capture: CaptureRecord, artifact_dir: Path,
                      options: RestoreOptions, deadline: float) -> BackendSession: ...
    async def cancel(self, handle: OperationHandle) -> CancelOutcome: ...
    async def cleanup(self, session: BackendSession | None, path: Path | None) -> None: ...
```

### Data contracts

| Type | Fields |
|---|---|
| `BackendCapabilities` | `name`, `version`, `tool_paths` (absolute), `available`, `arch`, `kernel_release`, `self_check` (output summary, PLT-009), `requires_privileges` (list), `supports_leave_running`, `supports_cancel`, `supports_process_tree`, `supports_threads`, `restores_same_pid`, `verified` (bool, true only with Stage 02 evidence for this version and architecture), `notes` |
| `WorkloadSpec` | `workload_id`, `executable`, `argv`, `working_dir`, `env` (already filtered), `stdout_path`, `stderr_path` |
| `BackendSession` | `pid`, `pid_start_time`, `pgid`, `helper_pids` (for example a coordinator), `data` (JSON-serializable, backend-specific; stored in `ProcessInstance.backend_session`) |
| `SessionStatus` | `ALIVE`, `EXITED(code, signal)`, `UNKNOWN(reason)` |
| `CheckpointOptions` | `leave_running` (default true, FR-011), `include_children` (default true) |
| `CaptureResult` | `artifacts`: list of `{relative_path, role}`, where role is `IMAGE`, `RESTART_SCRIPT`, or `METADATA`, all inside `staging_dir`; `restore_hints` (JSON); `capture_ms`; `backend_log_excerpt` (at most 4 KiB) |
| `CaptureRecord` | the subset of the manifest that the backend needs for restore: `backend_version`, `artifacts`, `restore_hints` |
| `RestoreOptions` | `working_dir`, `stdout_path`, `stderr_path` |
| `CancelOutcome` | `CANCELLED`, `NOT_SUPPORTED`, `TOO_LATE` |

### Error contract

Every method raises `BackendError(code, message, retryable, detail)`. The codes:

| Code | Meaning |
|---|---|
| `BACKEND_UNAVAILABLE` | the tool is missing or the self-check failed |
| `UNSUPPORTED_WORKLOAD` | a known-unsupported condition, with reason codes |
| `PERMISSION_DENIED` | missing privilege, ptrace restriction, or file capability |
| `LAUNCH_FAILED` | the workload or helper failed to start |
| `CHECKPOINT_FAILED` | the engine reported failure, or produced no or partial artifacts |
| `RESTORE_FAILED` | the engine reported failure, or no process appeared |
| `RESOURCE_CONFLICT` | for example, a PID needed for restore is in use |
| `VERSION_MISMATCH` / `ARCH_MISMATCH` | the capture is incompatible with this engine or host |
| `TIMED_OUT` | the deadline passed. The adapter has killed its own helper subprocesses, but never the workload unless that was requested |
| `CANCELLED` | cancelled through `cancel` |
| `INTERNAL` | an adapter bug. Always logged with a traceback |

### Behavioural rules for every adapter

1. **Deadlines are mandatory.** Every engine invocation runs as a subprocess with an argument array, the absolute tool path from `probe`, a timeout, umask 077, and size-capped output capture (REL-008, SEC-004).
2. **Output paths stay contained.** The adapter writes only inside the `staging_dir` or `session_dir` it is given. It reports every artifact it produced. It never decides what is published; storage does.
3. **Success means a complete capture.** `checkpoint` returns successfully only if the engine reported success and every expected artifact exists as a regular file of non-zero size. Otherwise it raises `CHECKPOINT_FAILED`.
4. **The workload keeps running.** On any error or timeout, the adapter leaves the workload running if it was running before the call. If the engine cannot guarantee that, the adapter's capabilities must say so.
5. **Restore reports the real process.** `restore` returns the session of the process that is actually running. The recovery manager does the verification (FR-026).
6. **Cleanup is idempotent.** `cleanup` removes only paths the adapter itself created.
7. **No privilege escalation.** If the engine needs privileges the user has not granted, the adapter raises `PERMISSION_DENIED` (SEC-010).
8. **Probing has no side effects.** `probe` and `assess` start no workload and modify nothing.

## Why DMTCP and CRIU need different process management and privileges

All statements in this section are UNVERIFIED on the target until Stage 02. They come from each project's documented design and must be confirmed or corrected by experiment.

| Aspect | DMTCP (documented design) | CRIU (documented design) | NapGuard consequence |
|---|---|---|---|
| Mechanism | User-space. A library is preloaded into the process at launch (`dmtcp_launch`) and cooperates with a coordinator process. | Kernel-assisted. It uses ptrace, `/proc`, and parasite code injected into the target, and needs the kernel's checkpoint/restore features. | Different failure modes and probe steps (PLT-009). |
| How a workload must start | It must be started under `dmtcp_launch`. Processes not started that way cannot be checkpointed. | Can dump an existing process tree by PID, so special launch is not strictly required. | v1 always launches workloads itself under the chosen backend (EXC-008). The supervisor contract is the same for both. |
| Helper processes | A `dmtcp_coordinator` per computation. NapGuard should run one coordinator per workload on a private port or socket so workloads stay isolated. | No long-lived helper. `criu dump` / `criu restore` run per operation. With `restore -d` the restored tree detaches. | `BackendSession.helper_pids` and the cgroup or scope containment (OD-03) must include the coordinator. |
| Privileges | Designed to run as an ordinary user. | Historically root. Newer releases document a non-root mode that needs `CAP_CHECKPOINT_RESTORE` (and related capabilities) set on the `criu` binary, which is a system modification. Ubuntu's Yama `ptrace_scope` can also restrict dumping. | DMTCP fits SEC-010 directly. CRIU needs an ADR and explicit user approval before any capability change. Without that it is `PERMISSION_DENIED`. |
| PIDs on restore | Virtualizes PIDs, so the application sees its original PIDs. | Restores original PIDs, which can conflict with PIDs already in use unless PID namespaces are used. | Identity always comes from the workload and instance IDs (LIM-006). PID conflict is surfaced as `RESOURCE_CONFLICT`. |
| Checkpoint artifacts | Per-process image files plus a generated restart script. | An image directory containing many `*.img` files. | Opaque files to storage. Hashed and recorded by role. Layout recorded from Stage 02 evidence. |
| Leave running | Documented: checkpointing does not stop the computation. | Documented `--leave-running` option. | Both expected to satisfy FR-011. Must be verified. |
| Statically linked or setuid programs | Preload-based. Static and setuid binaries are expected to be unsupported. | Not preload-based. Constraints differ. | Encoded as compatibility rules once observed (CMP-006). |
| Open TCP connections | Has a plugin mechanism. Behaviour must be measured. | Documented `--tcp-established` option, with constraints. | LIM-003: report what is measured, promise nothing. |
| aarch64 support | Historically focused on x86. ARM64 status unknown. Ubuntu 24.04 arm64 package availability unknown. | Has an aarch64 port. Ubuntu 24.04 package availability unknown. | **Stage 02 must establish installability and basic operation on the actual VM before anything is built on it.** |

## Stage 02 feasibility protocol (input to Stage 02, not executed in Stage 01)

1. **Package availability.** Check archive availability read-only (`apt-cache policy dmtcp criu`). Any installation from the archive or a source build needs user approval and must be recorded.
2. **Engine self-check.** Run each engine's own check (`criu check`, or DMTCP's test suite or a trivial launch) and record the full output.
3. **Representative workloads.** Run CMP-001's classes. Test programs are small and unmodified (no checkpoint-aware code). Each prints observable progress, used only as verification evidence (PRF-004, EXC-006).
4. **Per-trial sequence.** Each trial runs these steps:
   1. Launch.
   2. Wait for progress P1.
   3. Checkpoint.
   4. Let it continue to P2, which is greater than P1.
   5. Kill the process.
   6. Restore.
   7. Confirm that progress resumes from a value at least P1 and at most P2, and never from zero.

   Record the commands, versions, timings, image sizes, and artifact listings for each trial.
5. **Cold boot.** Repeat the per-trial sequence across a cold VM reboot for at least one class.
6. **Outputs.** The compatibility matrix, the backend decision ADR, and corrections to this document.

Stage 02's gate (from the approved roadmap) is that a genuine checkpoint and restore succeeds. If no backend works on the VM, the project stops and reassesses the architecture or the target environment before Stage 03.
