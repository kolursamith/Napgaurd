# Stage 02 Report: Real Checkpoint Engine Feasibility

- Date: 2026-10-19
- Baseline: Stage 01 PASS (user-reported) at `39714383fcd9b683f367e7476882bfb7102adaa3` on `stage-01/requirements-design`
- Branch: `stage-02/checkpoint-feasibility`, created from the approved Stage 01 commit. The commit SHA is recorded in Git history and the Ubuntu logs.
- Verdict: **FEASIBILITY DEMONSTRATED; Stage 02 NOT formally complete.** DMTCP 4.2.0 `c-single` checkpoint/restore is **VERIFIED PASS on Ubuntu ARM64, both same-boot (section T) and cross-reboot (section W)**.
  - Other workload classes: NOT VERIFIED.
  - Production coordinator security: NOT APPROVED (ADR 0004 is Stage 02-only).
  - Evidence matrix and closure table: section X.

Stage 02 PASS needs Ubuntu ARM64 evidence that a backend checkpoints and restores a real running process with its execution state preserved, in the same boot and across a VM reboot. Section T records the same-boot evidence and section W the cross-reboot evidence (both user-reported). No checkpoint experiment was run on macOS.

Labels used below:

- **[V-src]**: verified by reading upstream source, documentation, or archive metadata on 2026-10-19.
- **[DOC]**: a documented claim by the upstream project that has not been tested by us.
- **[HYP]**: our hypothesis, to be tested on the VM.
- **[OPEN]**: an unresolved question.

---

## Ubuntu results to date (user-reported)

The user ran these on Ubuntu 24.04.5 LTS aarch64 at commit `4879432d7c59b4f5a3516b4f0cb4b165b58ac36a`. Claude did not see the raw logs.

| Check | Result |
|---|---|
| Stage 02 Linux safety tests (`test_stage02_safety.py`, including the Linux-only cases) | 13 passed |
| Full pytest with `PYTHONPATH` set manually | 86 passed, exit 0 |
| Plain `python3 -m pytest -q` | **FAILED:** `napguard` not importable (src layout, no install, no PYTHONPATH). Fixed: pytest `pythonpath = ["src", "experiments/stage02"]`, plus a regression test. |
| `python3 -I experiments/stage02/run_stage02.py env` | **FAILED:** `ngexp` not importable (`-I` drops the script's directory). Fixed: the launcher adds exactly its own resolved directory to `sys.path`, plus regression tests. |
| `run_stage02.py env` without `-I` | ran; **both backends BLOCKED, neither installed** |
| `kernel.apparmor_restrict_unprivileged_userns` | `1` |
| Experiment storage | `/dev/vda2`, ext4 (persistent) |

No checkpoint or restore has run on Ubuntu. Neither backend was installed at that commit. For the later DMTCP build and isolation evidence, see section O.

## A. Existing design audit

Inputs read before making changes:

- `AGENTS.md`
- [requirements](../requirements/requirements.md)
- [traceability](../requirements/traceability.md)
- [backend contract](../architecture/checkpoint-backend.md)
- [components](../architecture/components.md)
- [storage and recovery](../architecture/storage-recovery.md)
- [IPC and security](../architecture/ipc-security.md)
- the Stage 01 report's open decisions and risks

| Topic | What NapGuard requires (Stage 01) | Requirement IDs |
|---|---|---|
| Capabilities | probe, assess, launch under management, status, checkpoint that **leaves the workload running**, restore, cancel where supported, cleanup, typed errors, mandatory deadlines | FR-005, FR-007, FR-010, FR-011, FR-026, PLT-006, PLT-009, REL-008 |
| Privileges | daemon runs as the user with no added capabilities. Any backend privilege needs an ADR and approval. | SEC-010 |
| Process ownership | act only on processes NapGuard launched or restored, identified by pid + start time + boot ID | SEC-003, EXC-002 |
| Launch and attach | **launch-under-management only** in v1. No attach to unmanaged processes. | EXC-008 |
| Artifacts | native backend files stored opaquely, hashed, kept 0600, published atomically | SEC-006, REL-001, REL-006 |
| Restore | restored process verified alive and continuing. PID may change, identity is the workload ID. | FR-026, LIM-006 |
| Kernel dependencies | backend self-check recorded at startup | PLT-009 |
| Compatibility | evidence-based matrix over 7 workload classes. Claims only with evidence. | CMP-001, CMP-002, CMP-006 |
| Limits | TCP sessions, devices, changed files, and side effects are not guaranteed | LIM-003, LIM-004, LIM-007 |
| No substitutes | progress files are never a checkpoint | EXC-006 |
| Measurements | checkpoint and restore durations, image sizes, lost progress | PRF-001, PRF-002, PRF-003, PRF-004 |

Stage 02 also corrects the Stage 01 backend table (see the "Stage 02 research update" in the [backend contract](../architecture/checkpoint-backend.md#stage-02-research-update-2026-10-19)).

## B. DMTCP research findings

Sources: `github.com/dmtcp/dmtcp` (releases, tags, `README.md`, `INSTALL.md`, `configure.ac`, `src/*.cpp`, `src/mtcp/mtcp_restart.c`, `.github/workflows`) and the Launchpad Ubuntu archive API.

| # | Finding | Label |
|---|---|---|
| B1 | Latest release **v4.2.0** (2026-06-26), tag commit `f8009ce7b4ad211311ca2f72a929b975e4aa1155`. The README calls it an interim release; `main` targets 5.0. | V-src |
| B2 | The README lists support for "x86_64, i386, arm64, risc-v". The 3.1.0 notes say "Supports: x86_64, aarch64 (ARM64), RISC-V". 3.1.1 fixed an ARM64 SIGBUS caused by 16-byte alignment. | V-src (claim) / DOC (behaviour) |
| B3 | `configure.ac` detects aarch64 hosts and handles gcc atomics on ARM64. `src/mtcp/syscall-aarch64.S` exists. | V-src |
| B4 | `mtcp_restart.c` has `# warning … "TODO: Implementation for ARM64"` at the TLS reset in `mtcp_restart_new_stack()`. Its impact is unknown. | V-src / OPEN |
| B5 | Upstream CI (`make-check.yml`) runs only on `ubuntu-latest` (x86-64), so ARM64 is not continuously tested upstream. | V-src |
| B6 | Ubuntu archive: `dmtcp` was last published for bionic (2.3.1). Focal entries were deleted, and there is no noble package. This matches the VM's empty APT candidate. **No ARM64 binary package is available, so a source build is required.** | V-src |
| B7 | Build: `./configure && make && make install`. Prerequisites: `git gcc g++ make`, plus `python3` for tests. `--prefix` allows a user-owned install. | V-src (docs) |
| B8 | Model: a preloaded user-space library plus a coordinator. No kernel modules. Processes must be **started** with `dmtcp_launch`. | DOC |
| B9 | Privileges: designed for ordinary users. Checkpointing does not use ptrace. | DOC / HYP (ptrace not needed under Yama scope 1) |
| B10 | CLI options used by the harness exist in the v4.2.0 source: `--coord-host`, `--coord-port`, `--port-file`, `--join-coordinator`, `--ckptdir`, `--tmpdir`, `--bcheckpoint`, `--list`, and `--quit`. | V-src |
| B11 | **The standalone coordinator listens on `JSockAddr::ANY` (all interfaces)**, and we found no authentication in the coordinator protocol. Any host that can reach the port could issue commands. | V-src / security (I-1) |
| B12 | Images: `ckpt_*.dmtcp` (gzip-compressed by default) plus `dmtcp_restart_script*.sh`. PIDs are virtualized, so the restored process keeps its virtual PID and gets a new real PID. | DOC |
| B13 | It restores genuine execution state (memory, registers, threads) from its own images, not from application data. | DOC → HYP on ARM64 |
| B14 | Builds with GCC 13 / glibc 2.39 (Ubuntu 24.04). The 4.1.x notes show work for newer glibc (2.41, 2.42). | HYP |

Known limitations relevant to NapGuard:

- Static and setuid binaries are expected to be unsupported.
- GUI programs need VNC-style setups.
- Network peers outside the computation are not restored.
- The aarch64 TODO (B4).
- The network-exposed coordinator (B11).

## C. CRIU research findings

Sources: `github.com/checkpoint-restore/criu` (tags, `Documentation/criu.txt`, `INSTALL.md`, `criu/files-reg.c`), `criu.org/Installation`, and the Launchpad Ubuntu archive and `~criu` PPA APIs.

| # | Finding | Label |
|---|---|---|
| C1 | Latest tag **v4.2.1**, commit `9539417f3e3cfa4eb84c319cd71f4d52f1f08645`. Releases are published as tags. | V-src |
| C2 | Not in the Ubuntu 24.04 (noble) archive. The Ubuntu archive has 4.2-1ubuntu2 only in newer series, which are not usable on noble. | V-src |
| C3 | The CRIU team's PPA (`ppa:criu/ppa`) publishes **4.2.1-1ppa1.24.04 for noble/arm64**. Adding a PPA is a system change and needs approval. | V-src |
| C4 | Source build: build-essential, pkg-config, protobuf and protobuf-c (dev and compiler), python3-protobuf, libcap-dev, libnl-3-dev, libnet1-dev, libaio-dev, libgnutls28-dev, uuid-dev, libbsd-dev. | V-src (criu.org) |
| C5 | **Non-root:** requires `CAP_CHECKPOINT_RESTORE` (kernel ≥ 5.9) or `CAP_SYS_ADMIN`, **and** `CAP_SYS_PTRACE` or Yama `ptrace_scope = 0`. It must be run with `--unprivileged`. The docs describe "many limitations". | V-src (docs) |
| C6 | The kernel side requires checkpoint/restore support (`CONFIG_CHECKPOINT_RESTORE`) and access to `/proc/<pid>/map_files`. The harness records the kernel config and `criu check` output. | DOC |
| C7 | **File validation:** for every open regular file, CRIU stores its size at dump and refuses restore if the size differs (`files-reg.c`, `store_validation_data` and the "has bad size" check). | V-src |
| C8 | `--leave-running` is documented as dangerous, because the process keeps changing files after the dump. | V-src (docs) |
| C9 | Restores **original PIDs** without PID namespaces, so restore fails if a PID is in use. After a reboot, low PIDs may already be taken. | DOC / HYP (cross-reboot risk) |
| C10 | Model: dumps an existing tree by PID through ptrace, with parasite code inside the target. Attaching to a process is possible in principle, but NapGuard v1 excludes it (EXC-008). | DOC |
| C11 | It restores genuine execution state from its own images. | DOC → HYP on this VM |

**Incompatibility with the user-level model.** As an ordinary user under Ubuntu's default `ptrace_scope = 1`, CRIU cannot checkpoint without either:

- **(a)** granting the binary `CAP_CHECKPOINT_RESTORE` and `CAP_SYS_PTRACE`. This is security-sensitive (I-2) and needs an ADR and approval; or
- **(b)** running it as root. That is a design change, and SEC-010 forbids it without approval; or
- **(c)** setting `ptrace_scope = 0` globally. That weakens system security, which this stage forbids.

## D. Reproducible Linux experiment design

The harness lives in `experiments/stage02/` (see its [README](../../experiments/stage02/README.md)). It uses the standard library only and runs only on Linux; on any other OS it exits BLOCKED.

| Requirement | How it is met |
|---|---|
| Linux only; records arch and kernel | `platform.system()` guard. `env` and every `result.json` record os-release, `uname -m`, the kernel, `CONFIG_CHECKPOINT_RESTORE`, `ptrace_scope`, the AppArmor userns restriction, `pid_max`, `CapEff`, uid, boot ID, and the filesystem and device of the run root. |
| Backend versions and capabilities | `probe()` records tool paths and versions. For CRIU it also records `getcap`, ptrace scope, and `criu check` output. |
| Dedicated experiment directories | One directory per run, `~/.local/share/napguard-stage02/runs/<utc>-<backend>-<class>-<rand>/` (0700). The directory is persistent on purpose, so evidence survives and the reboot test works. |
| Diagnostic logs kept | `commands.jsonl` (argv, exit code, duration, log paths), `events.jsonl`, `logs/NNN-*.out/err`, the CRIU `-v4` logs, `reports/*.txt`, and `result.json` |
| Bounded timeouts | Every command: probes 15 s, checkpoint/dump/restore 180 s, build 120 s. Waits are 15–30 s. A timeout kills only that command's own process group. |
| Cleanup limited to its own processes | Each process starts in its own session, and its identity (pid, `/proc` start ticks, uid) is recorded. Signals are sent only when the identity matches, so a reused PID is never touched. Restored processes are adopted only if they are owned by the user. A final mandatory check fails if any tracked process survives. |
| Never checkpoints unrelated processes | DMTCP checkpoints only processes connected to a private coordinator on a kernel-assigned port. CRIU dumps only the PID the harness spawned. |
| No destructive system changes | No sudo unless `--criu-mode sudo` is passed. No sysctl changes, no package installation, no reboot. |
| Exact commands, exit codes, artifacts | `commands.jsonl` records every command. `result.json.facts.artifacts` lists each artifact's path, size, SHA-256, and mode, and the filesystem it is on. |
| PASS / FAIL / SKIP / BLOCKED | `ngexp.results.overall()`: a mandatory FAIL beats BLOCKED, which beats PASS. **A run with no executed mandatory PASS is never reported PASS.** A missing backend or missing privilege is BLOCKED. |

Trial sequence (`trial`, `matrix`): build the workload, then probe the backend, then:

1. **Fresh-start control.** The workload runs without any backend.
2. **Launch** under the backend.
3. **Checkpoint**, with marks recorded just before (`ckpt_lower`) and just after (`ckpt_upper`).
4. **Continuation check:** the workload must keep running after the checkpoint (FR-011).
5. **Run on** for 4 s.
6. **Terminate** with SIGKILL, and confirm every original process is gone (`kill` marks).
7. **Persistence check:** artifacts must be on a persistent filesystem (ext4, xfs, or btrfs).
8. **Restore** with the backend's own command.
9. **Observe** for 3 s.
10. **Verify** each role, then **clean up**.

Workload classes implemented, each mapped to CMP-001:

| Class | Content |
|---|---|
| `c-single` | single-threaded C, 4 MiB ballast |
| `c-threads` | 3 worker threads |
| `c-child` | forked child process |
| `c-file-static` | holds a written file open; the file is unchanged after the dump |
| `c-file-grow` | holds a file open and keeps appending to it (LIM-007 probe) |
| `py` | Python computation, 1 MiB ballast |
| `tcp`, `gui` | **SKIP, not implemented yet.** This is reported honestly, not as PASS. |

## E. Real in-memory workload

`workload/ngcounter.c` is an ordinary C program with no checkpoint awareness. Its whole state is in memory:

- a 128-bit nonce from `getentropy()` at startup;
- for each role (main thread, worker threads `tN`, forked `child`) a counter that advances every 10 ms, and a digest chain `d_k = splitmix64(d_(k-1) ^ k)` seeded from the nonce;
- optionally a ballast buffer filled from the nonce and never written again. Its digest is recomputed **from memory** at every report.

**Observation.** Every report, each role appends one `NGSTATE` line to a report file using `open(O_APPEND)`, `write`, `close`. No descriptor stays open between reports, which avoids C7 for the main classes. The line also carries the kernel PID, obtained with a raw `svc`/`syscall` instruction, because DMTCP virtualizes `getpid()`.

**No persistence.** The program never reads the report file or any other file. It has no restore, load, or resume code path. A unit test checks that the source contains no read or mmap calls. `workload/ngcounter.py` implements the same model in Python.

How the evidence distinguishes each state:

| State | Observable evidence |
|---|---|
| Fresh startup | new nonce, counter starts at 0 (fresh-start control check) |
| Before checkpoint | counters up to `ckpt_lower` with the original nonce |
| At checkpoint | the checkpointed counter is inside `[ckpt_lower, ckpt_upper]` |
| After termination | the original reached `kill`, then every original process was confirmed dead |
| After restore | **same nonce**, first counter in `[ckpt_lower, kill)` (rolled back, neither 0 nor continued), digests valid, ballast intact, counter advancing |

The report lines are evidence only. The harness never feeds them back into the workload, and verification recomputes every digest independently (`ngexp/verify.py`).

## F. Mac test results

These ran on macOS 14.8.1 arm64 with Python 3.14.7 (the repository `.venv`). **They are not Linux or backend evidence.** No checkpoint experiment was run on macOS.

| Command | Exit | Result |
|---|---|---|
| `.venv/bin/python -m pytest` | 0 | `83 passed, 6 skipped`. The 6 skipped tests are Linux-only safety tests; they run on Ubuntu, where the user reported the earlier 3 passing. |
| `.venv/bin/ruff check .` | 0 | `All checks passed!` |
| `.venv/bin/mypy` (all configured files, Linux platform stubs) | 0 | `no issues found in 27 source files` |
| `.venv/bin/mypy src tests` (the form the Ubuntu baseline runs) | 0 | `no issues found in 17 source files` |
| `python3 -I experiments/stage02/run_stage02.py env` | 2 | `BLOCKED: Stage 02 experiments run only on Linux` (correct refusal) |
| `bash build-dmtcp.sh`, `bash build-criu.sh` | 1 | `run this inside Ubuntu` (correct refusal); `bash -n` syntax OK |
| `cc -Wall -Wextra -Werror -pthread workload/ngcounter.c` | 0 | warning-free under Apple clang. GCC on Ubuntu is not yet tested. |

The Stage 02 tests:

- **`test_stage02_model`:** splitmix64 reference vectors, the digest chain, role salts, parsing, and malformed-line rejection.
- **`test_stage02_verify`:** a genuine restore passes. Each of these fails:
  - a fresh start detected by a new nonce;
  - a counter reset;
  - state that did not roll back;
  - a tampered digest;
  - no progress after restore;
  - the original not confirmed dead;
  - bad marks;
  - ballast corruption.

  The status aggregation never reports PASS without an executed PASS.
- **`test_stage02_backends`:** DMTCP argument vectors use a private loopback coordinator with `--join-coordinator`. CRIU dumps only `--tree <pid>` with `--leave-running`, and sudo appears only in the explicit mode. `getcap` parsing.
- **`test_stage02_workload`:** the C workload (main, 2 threads, forked child, 1 MiB ballast) and the Python workload, run as plain processes, match the Python model exactly: digests, ballast, one nonce, counters from 0. The workload sources contain no read or mmap paths.

- **`test_stage02_safety`:**
  - `/proc` stat parsing with a hostile `comm`.
  - Loopback-listener detection, where empty output is not treated as safe.
  - The DMTCP probe reports BLOCKED, and launch and restore spawn nothing.
  - The build script refuses `make check`.
  - Linux-only: group termination also kills orphaned members of the run's own group; adoption refuses the test process, its parent, a foreign session, and a process started before the cut-off; a pidfd signal with the wrong start time is not delivered.

Problems found and fixed during this stage:

- **The C workload hung on SIGTERM when `--fork-child` was used.** The parent waited for a child that was never told to stop. The parent now signals the child before waiting, and the test checks that the workload exits cleanly.
- **A Ruff import-order fix** (I001) was needed once the experiment package became first-party.
- **One mypy type error** in the matrix summary was fixed.
- **Review fixes before commit:**
  - The workload test now kills its whole process group if a test fails, so a forked child cannot be orphaned.
  - The DMTCP restore check no longer reaps the restarter. Reaping it would free its group number while members might remain.
  - `reboot-resume` re-seeds the run's session ids from the manifest and gates adoption on start time.
  - The DMTCP coordinator refusal (I-1a) was added.

Earlier configuration change: `pyproject.toml` now type-checks against Linux stubs (`platform = "linux"`, since NapGuard is Linux-only), adds `experiments/stage02` to the mypy, Ruff, and pytest paths, and sets `mypy_path`.

## G. Exact Ubuntu setup and test commands

Run these in the VM as `samith`, from `~/napguard`. **Steps marked APPROVAL need your explicit decision before you run them.**

```sh
# 0. Exact revision (after the Stage 02 commit is approved and pushed)
git fetch origin && git checkout <stage-02 branch> && git pull --ff-only
git rev-parse HEAD                                   # must equal the Mac commit SHA
bash scripts/layer0-ubuntu-check.sh <MAC_COMMIT_SHA> 2>&1 | tee ~/stage02-baseline.log

# 1. Environment and backend availability (read-only)
python3 -I experiments/stage02/run_stage02.py env 2>&1 | tee ~/stage02-env.log

# 2. DMTCP prerequisites. APPROVAL: system packages (skip any that are already installed)
sudo apt install git gcc g++ make python3

# 3. DMTCP source build as the user (pinned v4.2.0; ~1 GiB disk). Default 2 parallel make jobs;
#    override with NAPGUARD_STAGE02_BUILD_JOBS=<positive integer>. `--check-config` only validates.
bash experiments/stage02/build-dmtcp.sh 2>&1 | tee ~/stage02-build-dmtcp.log

# 4. DMTCP same-boot trial, then the class matrix.
#    CURRENTLY REFUSED by the harness (BLOCKED, I-1a / OD-13): no coordinator is started.
#    These become runnable only after OD-13 is resolved and approved.
python3 -I experiments/stage02/run_stage02.py trial --backend dmtcp --class c-single 2>&1 | tee ~/stage02-dmtcp-single.log
python3 -I experiments/stage02/run_stage02.py matrix --backend dmtcp 2>&1 | tee ~/stage02-dmtcp-matrix.log

# 5. CRIU build dependencies. APPROVAL: system packages
sudo apt install build-essential git pkg-config libprotobuf-dev libprotobuf-c-dev \
  protobuf-c-compiler protobuf-compiler python3-protobuf libcap-dev libnl-3-dev \
  libnet1-dev libaio-dev libgnutls28-dev uuid-dev libbsd-dev
bash experiments/stage02/build-criu.sh 2>&1 | tee ~/stage02-build-criu.log

# 6. CRIU as an ordinary user, with no capabilities. Expected: BLOCKED (records why)
python3 -I experiments/stage02/run_stage02.py trial --backend criu --class c-single 2>&1 | tee ~/stage02-criu-user.log

# 7. Optional CRIU diagnostics. APPROVAL each; see section I before deciding
#   7a capability grant to the one user-owned binary:
#      sudo setcap cap_checkpoint_restore,cap_sys_ptrace+ep ~/.local/opt/napguard-stage02/criu-4.2.1/sbin/criu
#      python3 -I experiments/stage02/run_stage02.py matrix --backend criu
#   7b root diagnostic (does not match the user-level model):
#      sudo -v && python3 -I experiments/stage02/run_stage02.py --criu-mode sudo matrix --backend criu

# 8. Reboot recovery: section J

# 9. Collect evidence (result files and logs; checkpoint images excluded to keep it small)
tar -C ~/.local/share/napguard-stage02 --exclude='*/checkpoint/*' -czf ~/stage02-evidence.tgz runs env-latest
```

Paste the logs from steps 0, 1, 3, 4, 6, and 8 (and 7 if run), or share `~/stage02-evidence.tgz`.

## H. Backend capability matrix

Status on Ubuntu 24.04 aarch64 (UTM): every execution cell is **NOT RUN**.

| Capability | DMTCP 4.2.0 | CRIU 4.2.1 |
|---|---|---|
| ARM64 build available | No Ubuntu package (B6). Source build expected to work [DOC B2] | PPA package for noble/arm64 [V-src C3]; source build [C4] |
| Runs as ordinary user | Yes [DOC B8, B9] | **No.** Needs file capabilities or root [V-src C5] |
| Launch model | must start under `dmtcp_launch` | can dump an existing tree; v1 launches it itself |
| Helper process | coordinator, listens on all interfaces [V-src B11] | none persistent |
| Native artifacts | `ckpt_*.dmtcp` + restart script | `*.img` directory |
| Leave running after checkpoint | documented | `--leave-running` (documented as dangerous) |
| Restored PIDs | virtualized (new real PID) | original PIDs (conflict risk) |
| Open growing files | unknown, measured by `c-file-grow` | expected restore failure [V-src C7] |
| Threads / child processes | documented | documented |
| Python | documented | documented |
| TCP / GUI | not tested in Stage 02 (SKIP) | not tested in Stage 02 (SKIP) |
| Same-boot restore on VM | **PASS for `c-single` only** (section T); other classes not run | **NOT RUN** (privileges, OD-14) |
| Cross-reboot restore on VM | **PASS for `c-single` only** (section W) | **NOT RUN** |

## I. Security and privilege findings

| # | Finding | Consequence for NapGuard |
|---|---|---|
| I-1 | The DMTCP standalone coordinator listens on all interfaces with no authentication found [B11]. (`--no-coordinator`, mentioned below, turned out not to exist in v4.2.0; see I-1a.) | Production must not expose it. Options to evaluate in Stage 03 (OD-13): a firewall rule (system change, needs approval); `--no-coordinator` mode (single-process only); a network namespace (may be blocked by Ubuntu's AppArmor restriction on unprivileged user namespaces, which `env` records); a patched or newer coordinator. The harness records `ss -ltn` for each coordinator port as evidence. |
| I-1a | **Stage 02 review result (first pass):** no supported option restricts DMTCP 4.2.0's coordinator to loopback. Both the standalone coordinator (`dmtcp_coordinator.cpp:1745`) and the one `dmtcp_launch` starts (`coordinatorapi.cpp:490`) bind `JSockAddr::ANY` (INADDR_ANY), on the v4.2.0 tag and on `main`. There is no bind-address option. `--no-coordinator` was removed (only a stale comment and `INSTALL.md` mention it). The one other path is an undocumented internal one: a pre-opened listener handed over on the internal descriptor `PROTECTED_COORD_FD`. | **The harness refuses to start any DMTCP coordinator.** `probe()` reports BLOCKED. `launch()` and `restore()` raise before spawning anything, and a unit test checks that nothing is spawned. `--join-coordinator` keeps `dmtcp_launch` and `dmtcp_restart` from starting a coordinator themselves. Building DMTCP and `dmtcp_launch --version` start no coordinator (`--version` exits during argument parsing, `dmtcp_launch.cpp:237–239`). `build-dmtcp.sh` refuses `make check`, because DMTCP's own tests start coordinators. Candidate resolutions for OD-13, each needing review and approval: (1) a loopback-only network namespace for coordinator plus workload, if unprivileged user namespaces work under Ubuntu 24.04's AppArmor restriction (unverified); (2) a reviewed local patch, or use of the `PROTECTED_COORD_FD` mechanism with a NapGuard-created 127.0.0.1 listener (relies on an undocumented interface; needs an ADR); (3) a host firewall rule for the coordinator port (system change); (4) asking upstream for a bind-address option. |
| I-1b | **Isolation investigation (follow-up, see section N).** Unprivileged network namespaces are expected to fail under `apparmor_restrict_unprivileged_userns=1` [HYP; the `env` probe now records it]. The narrow alternative is DMTCP's own listener hand-off: a 127.0.0.1 listener pre-bound by the harness and passed on fd 821. It is implemented to fail closed and is exercised only by `dmtcp-isolation-check`. | **Trial gate still closed.** `DMTCP_LOOPBACK_ONLY_COORDINATOR_VERIFIED = False` until `dmtcp-isolation-check` passes on Ubuntu and the approach is approved by ADR. |
| I-1c | **Second DMTCP exposure path:** at restart, if the restored process had IPv4 sockets, the socket plugin opens a temporary restore listener on `INADDR_ANY` (`plugin/ipc/socket/connectionrewirer.cpp:192`). | The Stage 02 classes have no sockets, and the `tcp` class stays unimplemented. The trial network guard (section N) aborts and kills everything if any experiment process listens beyond loopback after launch, checkpoint, or restore. |
| I-2 | Non-root CRIU needs `CAP_SYS_PTRACE` (under `ptrace_scope=1`) and `CAP_CHECKPOINT_RESTORE` on its binary [C5]. Anyone able to execute that binary could ptrace and dump processes of **any** user, so this is effectively root-equivalent. | It conflicts with SEC-010. Adopting it needs an ADR, explicit approval, and an execute-restricted binary (0750, in a 0700 directory). Recommended only for diagnostics. |
| I-3 | `ptrace_scope` stays at the Ubuntu default. Nothing in Stage 02 changes it. | DMTCP is expected to be unaffected (B9). CRIU is affected. |
| I-4 | A checkpoint image is executable process state. Restoring a modified image runs attacker-chosen code as the user. | Restore only images whose recorded hashes match (SEC-007). `reboot-resume` refuses to restore on any SHA-256 mismatch. |
| I-5 | Images contain full process memory, including any secrets. | The run directories are 0700, and artifact modes are recorded. Production enforces 0600 files (SEC-006). Shared evidence excludes the images (G step 9). |
| I-6 | Corrupted or missing artifacts | The harness re-hashes before the cross-boot restore and reports FAIL without attempting it. Corruption-injection tests belong to Stage 04 (REL-006). |
| I-7 | Timeouts and cleanup | Bounded per command. A process group is signalled only while its leader is the harness's own **unreaped** child, which keeps the group number allocated, so no foreign group can be hit. This also kills members orphaned by the leader. Processes the harness did not spawn are adopted only if they are owned by the user, are **in a session this run created**, and **started no earlier than the launch or restore command**. They are signalled through a **pidfd** after re-checking pid, start time, and uid. CRIU dumps only after re-verifying the launched PID's identity. A survivor makes the trial FAIL (D). |
| I-8 | Unsupported workloads | Each class has its own PASS/FAIL/SKIP and reasons. Unimplemented classes are SKIP, never PASS (CMP-002). |
| I-9 | CRIU `--leave-running` and DMTCP checkpoints both let the process keep modifying files after the checkpoint. | The restored state may disagree with files on disk (LIM-007). The `c-file-grow` class measures this. |

## J. Reboot recovery procedure (manual; nothing reboots automatically)

1. Choose the backend that passed its same-boot trial, for example DMTCP:

   ```sh
   python3 -I experiments/stage02/run_stage02.py reboot-prepare --backend dmtcp --class c-single 2>&1 | tee ~/stage02-reboot-prepare.log
   ```

   This step:
   - launches the workload, takes a real checkpoint, and records the marks;
   - checks that the artifacts are on ext4 (`/dev/vda2`);
   - kills the original;
   - writes and fsyncs `reboot-manifest.json` with the boot ID and artifact SHA-256s, then calls `sync`;
   - prints the run directory.

2. **APPROVAL: you decide to power off.** Shut the VM down completely with `sudo systemctl poweroff`, so RAM is cleared, then start it again from UTM and log in as `samith`.

3. Resume:

   ```sh
   cd ~/napguard
   python3 -I experiments/stage02/run_stage02.py reboot-resume --run-dir <dir printed in step 1> 2>&1 | tee ~/stage02-reboot-resume.log
   ```

   The result is PASS only if all of these hold:
   - the boot ID changed (otherwise BLOCKED);
   - every artifact's SHA-256 is unchanged;
   - the backend restores the workload;
   - the restored workload shows the **pre-reboot nonce**, a counter inside `[ckpt_lower, kill)`, valid digests and ballast, and continued progress.

For CRIU, a cross-reboot restore may fail if the original PID is in use after boot (C9). That would be recorded as a real limitation.

## K. Known blockers

| # | Blocker | Resolution |
|---|---|---|
| K-0 | **DMTCP trials are refused by the harness** (I-1a): no supported loopback-only coordinator option. | The loopback fd hand-off (section N) needs: DMTCP built, `dmtcp-isolation-check` PASS on Ubuntu, then an ADR plus approval to open the trial gate. Until then, DMTCP can be built and checked, but no trial runs. |
| K-1 | No backend is installed on the VM, and neither has an APT candidate. | DMTCP: source build (G step 3), which needs compilers (APPROVAL for `apt install`). CRIU: source build or PPA (APPROVAL). |
| K-2 | CRIU cannot run as an ordinary user without a privileged capability grant [C5, I-2]. | Decision OD-14: grant capabilities (with an ADR), run a root diagnostic only, or set CRIU aside. |
| K-3 | No Linux experiment has run, so every capability cell is NOT RUN. | Run section G. |
| K-4 | The reboot needs your approval. | Section J. |
| K-5 | DMTCP on ARM64 is not CI-tested upstream, and there is an aarch64 TODO in the restart path [B4, B5]. | Only the trials can tell. If DMTCP fails, see L. |
| K-6 | The `tcp` and `gui` classes are not implemented. | They do not block the gate (the gate needs one genuine restore plus a cross-reboot restore). Follow-up within Stage 02 if required. |

New open decisions:

| ID | Decision |
|---|---|
| OD-13 | How to keep the DMTCP coordinator from being reachable over the network |
| OD-14 | CRIU privilege path (capabilities, root diagnostic, or not used) |
| OD-15 | Approval of the `apt` installs (compilers, CRIU -dev packages, optional PPA) |
| OD-16 | Approval of the VM power-off for the reboot test |

## L. Recommended backend (provisional until the Linux experiments pass)

**Provisional primary: DMTCP 4.2.0, built from source as the user, conditional on resolving OD-13.** Until a loopback-only coordinator is possible, the harness will not run it (I-1a).

- It is the only candidate that matches the approved user-level model without granting privileges (SEC-010).
- It claims aarch64 support.
- Its tools take everything NapGuard needs (a private coordinator, blocking checkpoint, restart).
- Main risks:
  - ARM64 is not CI-tested upstream (K-5).
  - The network-exposed coordinator (I-1), which must be solved before Stage 03 depends on it.

**CRIU: secondary or diagnostic only.** It is a capable engine, but it requires either root or a root-equivalent capability grant. Choosing it would be a design change that needs an ADR and your approval.

**If DMTCP fails on the VM**, these alternatives keep NapGuard's goal unchanged, in order of preference:

1. CRIU with the capability-granted binary, under an approved ADR (I-2).
2. A small privileged CRIU helper (a root systemd service with a narrow, authenticated IPC surface). This changes SEC-010 and needs an ADR.
3. Run NapGuard's backend on an x86-64 Ubuntu VM or host, where DMTCP is CI-tested. Support statements would then name x86-64 (PLT-001, PLT-008).
4. Narrow the supported workload classes to those that pass.

If none of these work, Stage 02 is BLOCKED and the target environment must be reassessed before Stage 03.

## M. Stage verdict

| Gate criterion | Evidence | Result |
|---|---|---|
| Design audit with traceability | section A | DONE (review) |
| Research separating facts, claims, hypotheses | sections B and C | DONE (review) |
| Harness, workload, and unit tests | `experiments/stage02/`, `tests/unit/test_stage02_*.py`, section F | DONE (Mac) |
| A backend installs on Ubuntu 24.04 aarch64 | `build-dmtcp.sh` (DMTCP 4.2.0, pinned source), user-reported | PASS (DMTCP) |
| Genuine checkpoint and restore with preserved in-memory state (same boot) | run `20261020T093355Z-dmtcp-c-single-7bce`, `TRIAL_EXIT=0` (section T) | **PASS (DMTCP, c-single only)** |
| Cross-reboot restore | run `20261020T100428Z-reboot-dmtcp-c-single-ed7a`, `PREPARE_EXIT=0`, `RESUME_EXIT=0` (section W) | **PASS (DMTCP, c-single only)** |

| No DMTCP coordinator exposed on the network | harness refusal + `test_stage02_safety.py` | ENFORCED (refuses to start) |

**Stage 02: feasibility demonstrated, not formally complete.** DMTCP `c-single` checkpoint/restore is VERIFIED PASS both same-boot (section T) and cross-reboot (section W). Closure needs the owner decisions in section X. Stage 03 implementation has not started; its plan is in [STAGE-03-plan.md](STAGE-03-plan.md).

## N. DMTCP coordinator isolation investigation (OD-13)

Status: **mechanism implemented and fail-closed; not yet verified on Ubuntu; trial gate closed.**

### Options evaluated

| Option | Assessment |
|---|---|
| Unprivileged user + network namespace (`unshare -Urn`, `bwrap --unshare-net`, systemd `PrivateNetwork=`) | On Ubuntu 24.04 with `apparmor_restrict_unprivileged_userns=1` (confirmed on the VM), an unconfined unprivileged process may create a user namespace, but it is confined by AppArmor without capabilities inside it. So it cannot create or configure a network namespace (`lo` up needs `CAP_NET_ADMIN`) [HYP]. Making it work would need a root-installed AppArmor profile granting `userns` to a specific binary (a system change needing approval), or disabling the restriction (weakens security system-wide; refused). The `env` command now records `unshare` and `bwrap` probe results, which create and discard a namespace and change nothing, as evidence. |
| Firewall rule | A system change. Refused under the current instructions. |
| Patching DMTCP | A narrow change, but it modifies third-party code. It needs an ADR and must be re-applied on every upgrade. Kept as a fallback. |
| **Loopback listener hand-off (implemented)** | DMTCP's coordinator first checks for an inherited listener on `PROTECTED_COORD_FD` (= 821; `include/protectedfds.h`) and uses it (`dmtcp_coordinator.cpp:1738-1742`). `dmtcp_launch --new-coordinator` uses exactly this hand-off internally (`coordinatorapi.cpp:490-508`), so it is DMTCP's own mechanism, but **not a documented user option**. |

### How the hand-off is made fail-closed

The steps below are in `ngexp/backends.py`, `Dmtcp._loopback_coordinator`.

1. **Version pin.** The hand-off is used only if `dmtcp_launch --version` reports the analysed version 4.2.0.
2. **Pre-bound listener.** The harness binds `127.0.0.1:0` and listens (no `SO_REUSEPORT`). Call the assigned port P.
3. **Wildcard-bind probe.** Before starting anything, the harness checks that binding `0.0.0.0:P` fails with `EADDRINUSE`. On Linux, a listening socket on `127.0.0.1:P` conflicts with a wildcard bind on P. If the kernel allowed the bind, the harness refuses to continue.
4. **Spawn with the same port.** The coordinator gets the listener as fd 821, with `--coord-port P`. If it ignored fd 821, it would fall back to `bind(INADDR_ANY:P)`, which fails, and the coordinator exits. The harness keeps its own copy of the listener open, so this holds even if the coordinator closed fd 821.
5. **Port check.** The port file must equal P; otherwise the coordinator is killed and the step fails.
6. **Post-start evidence.** `ss -H -l -n -p` must show only loopback listeners on P, and no non-loopback listener owned by the coordinator. TCP connects to every non-loopback IPv4 address of the VM on P must fail. Otherwise the coordinator is killed and the step fails.
7. **Trial guard.** After launch, checkpoint, and restore, any non-loopback TCP or UDP listener owned by an experiment process aborts the trial and kills everything (this covers I-1c).

Unit tests:

- **Portable:** `ss` parsing, violation detection, the `--coord-port` value, and that the trial gate stays closed even when the hand-off works.
- **Linux-only:** the wildcard bind is refused next to the loopback listener; a fake coordinator that uses fd 821 is confined; a fake coordinator that ignores fd 821 and tries `INADDR_ANY:P` **fails closed, with no exposed listener**.

### Residual risks

- **Undocumented interface.** The hand-off relies on an internal descriptor number. This is mitigated by the version pin and by fail-closed behaviour; it must be re-verified on any DMTCP upgrade.
- **No local authentication.** Loopback confinement removes **network** exposure only. Any local user on the same machine can still connect to `127.0.0.1:P` and send commands, such as kill or checkpoint. This is acceptable for the single-user Stage 02 VM, but **not sufficient for NapGuard production on multi-user hosts** (SEC-002). Stage 03 needs a design for it, for example per-user network isolation through an approved mechanism, or upstream authentication.
- **Restore-time listener (I-1c).** It is guarded, and TCP workloads stay excluded.

### Commands (Ubuntu, after `build-dmtcp.sh`)

```sh
python3 -I experiments/stage02/run_stage02.py env 2>&1 | tee ~/stage02-env.log                          # includes netns probes
python3 -I experiments/stage02/run_stage02.py dmtcp-isolation-check 2>&1 | tee ~/stage02-dmtcp-isolation.log
```

`dmtcp-isolation-check` starts one coordinator under the fail-closed hand-off, records the evidence, runs `dmtcp_command --status` over loopback, and stops the coordinator. It runs no workload and takes no checkpoint.

### Recommendation

1. Do not use network namespaces or firewall changes.
2. Build DMTCP and run `dmtcp-isolation-check` on the VM.
3. If it passes, record an ADR that approves the hand-off for **Stage 02 experiments only**, then open the trial gate with explicit approval.
4. Treat local cross-user access to the coordinator as an open production requirement for Stage 03.

### Review decision (2026-10-19)

- **Approved:** commit and push of the import fixes, regression tests, documentation, and the experimental `dmtcp-isolation-check`.
- **Not approved:** enabling DMTCP checkpoint trials, changing Ubuntu security settings, or treating loopback isolation as production-safe. `DMTCP_LOOPBACK_ONLY_COORDINATOR_VERIFIED` stays `False`. Unit tests check that the trial, matrix, and reboot-prepare entry points report BLOCKED and spawn nothing.

Pre-commit hardening of the isolation probe (it now fails closed on audit errors):

- If the listener audit cannot run (`ss` or `ip` missing, failing, or timing out), the coordinator is killed and the step fails. The trial network guard behaves the same way.
- A failing `ip` command is an error, never an empty address list that would silently skip the external-connect test.
- A spawn failure (for example fd 821 in use) is recorded as a FAIL, not raised as an unhandled exception.
- The isolation audit runs once, inside the error handling.

## O. Ubuntu DMTCP isolation evidence (user-reported, commit a57149e0)

Recorded 2026-10-19. These are user-reported results; Claude has not reviewed the raw evidence files.

| Observed on Ubuntu 24.04 aarch64 | Value |
|---|---|
| Commit | `a57149e0bfb42233c1c473c69486cd8a4021c308` |
| Kernel (reported at Stage 03 entry) | `7.0.0-38-generic` |
| DMTCP | 4.2.0, built from the pinned source with `build-dmtcp.sh` |
| Command | `python3 -I experiments/stage02/run_stage02.py dmtcp-isolation-check` |
| Coordinator port and listener | 55703, `127.0.0.1:55703` |
| Non-loopback address tested | `192.168.64.3`: connection **refused** |
| `dmtcp_command --status` over loopback | succeeded |
| Coordinator stop and cleanup | coordinator stopped; all tracked experiment processes cleaned up |
| Checks and exit code | **5 PASS**, `ISOLATION_EXIT=0` |
| Evidence directory (on the VM) | `~/.local/share/napguard-stage02/runs/20261019T142320Z-dmtcp-isolation-check-618c` |

What this does **not** establish:

- **No checkpoint or restore has run.** No workload was launched, and nothing about checkpointing is verified.
- **Fail-closed behaviour against real DMTCP is not demonstrated.** The run shows DMTCP *adopted* the pre-bound listener. Fail-closed behaviour has been shown only by Linux unit tests with a fake coordinator, plus the real kernel's `EADDRINUSE` refusal in `test_wildcard_bind_is_refused_next_to_loopback_listener`. It has not been reported which commit those Linux-only tests last passed at.
- **Coverage is IPv4 only.** Only IPv4 non-loopback addresses were probed. The coordinator binds IPv4 only (`JSockAddr` is `sockaddr_in`), and the `ss` audit covers IPv6 listeners too.

## P. Exposure review: launch, checkpoint, restore

| Step | Network behaviour (source reading, DMTCP 4.2.0) | Protection in the harness | Status |
|---|---|---|---|
| Coordinator start (launch and restore each start one) | the listener is adopted from fd 821; the fallback `INADDR_ANY:P` bind is refused by the kernel | the fail-closed hand-off and per-coordinator audit (section N) | hand-off adoption **observed on Ubuntu** (section O); fail-closed path tested with a fake coordinator only |
| `dmtcp_launch --join-coordinator` | client connection to `127.0.0.1:P`; never starts its own coordinator | audit after launch | not yet run with real DMTCP |
| Workload under DMTCP | DMTCP's connection socket sits at protected fds (≥ 820) | the ADR 0004 precheck: one process, no sockets below fd 820 | not yet run |
| `dmtcp_command --bcheckpoint` | client connection only | audit after checkpoint | not yet run |
| `dmtcp_restart --join-coordinator` | **opens an `INADDR_ANY` restore listener only if the image contains workload IPv4/IPv6 sockets** (`connectionrewirer.cpp:173-207`). A UNIX restore socket is used for UNIX-domain connections. | the precheck guarantees no workload sockets at checkpoint; audits every ~0.5 s while waiting, and after restore | **source reading only. If wrong, exposure lasts at most until the next audit, then the trial aborts** |
| `ssh` plugin | listens on `INADDR_ANY` (`dmtcp_ssh.cpp:100`) | not used (no remote processes) | n/a |

## Q. Workload classes for a first experiment

| Class | Initial trial? | Reason |
|---|---|---|
| `c-single` | **Yes, only class under ADR 0004** | one process, one application thread, no sockets, no held files; 4 MiB ballast proves memory restoration |
| `c-threads` | Not yet; next candidate | no sockets, but restores multiple threads on ARM64 (B4 TODO in the restart path). Run after `c-single` passes. |
| `py` | Not yet; next candidate | interpreter process; no sockets expected, but the precheck would have to be shown on Ubuntu first |
| `c-file-static` | Not yet | open writable file; tests file-restore semantics (LIM-007) |
| `c-child` | **Blocked** | multi-process: the precheck requires a single process; process-tree restore and cleanup need separate review |
| `c-file-grow` | Blocked for now | studies the LIM-007 failure mode; not a feasibility gate |
| `tcp` | **Blocked** | workload sockets make DMTCP open an `INADDR_ANY` restore listener (I-1c) |
| `gui` | **Blocked** | X11/Wayland sockets; out of scope (EXC-001) |

## R. Proposed change for one controlled trial (uncommitted, awaiting review)

This is implemented in the working tree for review only and is not active by default:

- **`trial --adr-0004-controlled-trial`** is accepted for `--backend dmtcp --class c-single` only. Every other combination, and `matrix` or `reboot-*`, is refused (BLOCKED). The flag does not exist on other subcommands.
- **`Dmtcp(controlled_trial=True)`**: `probe()` returns PASS with an "ADR 0004" note, and `_coordinator()` starts coordinators **only** through the fail-closed loopback hand-off. `DMTCP_LOOPBACK_ONLY_COORDINATOR_VERIFIED` stays `False`.
- **`check_controlled_workload()` before checkpoint:** the managed group must contain exactly one process, and `workload_socket_fds(pid)` must be empty, ignoring DMTCP's fds 820–859. Otherwise the trial fails before any checkpoint.
- **`wait_for_roles_guarded()`:** listeners are re-audited about every 0.5 s while waiting for the restored workload, in addition to the audits after launch, checkpoint, and restore.
- **Ownership-independent audit (review fix):** a listener baseline is taken before the trial starts anything. Any new non-loopback listener aborts the trial, whatever its owner, so a restored process that left the experiment's session cannot hide one. If no baseline can be taken, the trial is BLOCKED.
- **IPv6 (review fix):** global-scope IPv6 addresses are now probed as well as IPv4.
- **Leak scan (review fix):** after cleanup, any live process of the user that references the run directory makes the trial FAIL. It is reported, not killed.
- **Version check (review fix):** the controlled-mode probe is BLOCKED unless DMTCP reports 4.2.0.
- **Tests:**
  - **Portable:** the exception's scope (7 refusal cases); that the flag exists only on `trial`; that the global gate is unchanged and the exception applies per instance; that a version other than 4.2.0 is refused before any spawn.
  - **Linux-only:** socket detection, which ignores only DMTCP's protected descriptors.

### Ubuntu verification procedure (after review, ADR approval, commit, and push)

The scripted procedure, `experiments/stage02/ubuntu-controlled-trial.sh <SHA> [--run-trial]`, does the following:

- Stops with **BLOCKED** at the first failed prerequisite, without attempting any checkpoint. The prerequisites are, in order: SHA, clean tree, baseline, the Linux-only security tests (none may be skipped), and a fresh isolation check.
- Runs the single controlled trial only with `--run-trial`.
- Compares before and after listener snapshots, and checks for leftover processes of the user.
- Writes evidence to `~/napguard-stage02-evidence-<utc>/`.

The manual equivalent:

```sh
cd ~/napguard && git fetch origin && git checkout stage-02/checkpoint-feasibility && git pull --ff-only
git rev-parse HEAD                                    # must equal the reviewed commit
bash scripts/layer0-ubuntu-check.sh <SHA> 2>&1 | tee ~/s02r-baseline.log
python3 -m pytest -q tests/unit/test_stage02_safety.py -rs 2>&1 | tee ~/s02r-safety.log   # no Linux-only test may be skipped
python3 -I experiments/stage02/run_stage02.py dmtcp-isolation-check 2>&1 | tee ~/s02r-isolation.log
# Only if the isolation check passes in this boot, and ADR 0004 is approved:
python3 -I experiments/stage02/run_stage02.py trial --backend dmtcp --class c-single \
    --adr-0004-controlled-trial 2>&1 | tee ~/s02r-dmtcp-c-single.log; echo "TRIAL_EXIT=${PIPESTATUS[0]}"
ss -H -l -n -t -u -p | tee ~/s02r-listeners-after.log # expect no experiment-owned listeners
```

Expected evidence:

- `controlled-trial preconditions` PASS;
- `native checkpoint created` PASS, with the image size and artifacts on ext4;
- `restored state continuity (main)` PASS (same nonce, counter rolled back into the checkpoint window, ballast intact);
- every `dmtcp_isolation` event shows only `127.0.0.1` listeners;
- no `network_exposure` event;
- `experiment processes cleaned up` PASS.

Anything else is reported as FAIL or BLOCKED. A PASS here would be **same-boot only**; a cross-reboot DMTCP trial needs an ADR 0004 amendment.

## S. First controlled DMTCP trial on Ubuntu: failure, root cause, fix

### Observed (user-reported, commit a088cd93b73368cf1088219de866fc527adc70f9)

`ubuntu-controlled-trial.sh … --run-trial` produced **TRIAL_EXIT=1**:

| Time (coordinator log) | Event |
|---|---|
| 14:51:34.611 | workload connected |
| 14:51:34.983 | checkpoint started; generation incremented; one peer |
| 14:51:35.019 | coordinator received `quit` |

- **Harness result:** `FAIL native checkpoint created — dmtcp_command reported success but no ckpt_*.dmtcp image exists`. The checkpoint directory was empty.
- **Safety checks:** all isolation and cleanup checks passed, and no new non-loopback listener appeared.
- **No checkpoint or restore evidence was produced.**

### Root cause (verified in the pinned DMTCP 4.2.0 source)

**`dmtcp_command --bcheckpoint` does not block in DMTCP 4.2.0.**

1. `dmtcp_command` sends the blocking prefix as a separate one-character command `b`, then sends `c` (`dmtcp_command.cpp`, `case 'b':`).
2. The coordinator builds `string cmd(1, hello_remote.coordCmd)`, so it sees the string `"b"`. But `handleUserCommand` sets `blockUntilDone` only for `"bc"`, so `"b"` is logged as an `unhandled user command` and answered with `ERROR_INVALID_COMMAND`. The `main` branch has the same check.
3. `c` then starts the checkpoint and is answered **immediately**. `dmtcp_command` overwrites its status with that reply and exits 0.

The harness treated exit 0 as completion. It found no image about 35 ms later, recorded FAIL, and stopped the original, which matches the `quit` at `.019`. **The harness bug was trusting exit 0.**

### Fix (`ngexp/ckptwait.py`, `Dmtcp.checkpoint`)

- **Plain request.** `--checkpoint` is sent, and its exit status counts only as "request accepted".
- **Bounded completion wait** (180 s, polling every 0.25 s). Completion requires **all** of the following:
  1. exactly one `ckpt_*.dmtcp` regular file (single process, ADR 0004), non-empty, newer than the request. DMTCP writes `<image>.temp` and renames it when done (`processinfo.h:114`, `ckptserializer.cpp:400-411`);
  2. no `*.temp` or `*.tmp` file left;
  3. a `dmtcp_restart_script_*.sh` newer than the request that names **this run's coordinator port and the image**. The coordinator writes it only after every worker has reported its image (`dmtcp_coordinator.cpp:575-595`);
  4. image sizes and mtimes unchanged for at least 0.5 s;
  5. afterwards, `dmtcp_command --status` reports `NUM_PEERS=1` and `RUNNING=yes`.
- **Each poll** runs the listener audit (an exposure aborts the trial) and liveness checks: the workload's identity must still be alive and the coordinator must not have exited. Either death fails at once.
- **On timeout or failure** the trial FAILs with diagnostics: the evidence reason, the directory listing, temporary and stale files, status fields, and the coordinator log lines containing "checkpoint", "Checkpoint complete", "unhandled user command", or errors. Normal cleanup then runs.
- **Restore wording.** Restore is not claimed early: the check is now `restore command started … restoration NOT yet verified`. Success is decided only by `restored state continuity` (same nonce, counter rolled back into the window, digests, ballast, progress).
- **Unchanged:** ADR 0004 restrictions (DMTCP 4.2.0, `c-single` only, loopback hand-off, no workload sockets); the global gate (`False`); listener audits.
- **Regression tests** (`tests/unit/test_stage02_ckptwait.py`):
  - Deterministic virtual-clock tests: a delayed checkpoint is **not** rejected early; a growing image must stabilise first; a missing image times out with diagnostics; a leftover `.temp` image, a missing restart script, a foreign port or image, two images, an empty image, and stale files all fail; workload death aborts at once; an audit failure propagates.
  - Linux-only: `Dmtcp.checkpoint` with a fake `dmtcp_command` that returns at once and finishes 1.5 s later passes. A checkpoint that never lands fails at the timeout. A workload killed mid-wait fails fast.

### Next Ubuntu step

Rerun the procedure in section R at the new commit. Nothing here claims that checkpoint or restore works.

## T. First genuine checkpoint/restore: VERIFIED PASS (same boot, DMTCP, c-single)

These are user-reported results from 2026-10-20. Claude has not reviewed the raw evidence files.

| Item | Observed on Ubuntu |
|---|---|
| Commit | `f0f6599976f5a7fdeefd8f51413821c5d2606516` |
| System | Ubuntu 24.04.5 ARM64, kernel `7.0.0-38-generic`, persistent ext4 |
| Backend | DMTCP 4.2.0 (pinned source build) |
| Stage 00 baseline | PASS |
| Linux security tests | 58 passed |
| Coordinator isolation (`dmtcp-isolation-check`) | PASS |
| Controlled trial | `trial --backend dmtcp --class c-single --adr-0004-controlled-trial`: **PASS**, `TRIAL_EXIT=0` |
| Native checkpoint image | 25,059,040 bytes |
| Checkpoint completion (verified wait, section S) | 1.21 s |
| Original process | terminated successfully |
| Nonce | `d87aa0152528587f4388e57a305040a7`, preserved across restore |
| Counter at original termination | 450 |
| First counter after restore | 40 (rolled back into the checkpoint window, not 0, below 450) |
| Counter after observing the restored process | 340 (advancing) |
| Network | no new non-loopback listeners |
| Processes | no leftover experiment processes |
| Evidence directory (VM) | `~/.local/share/napguard-stage02/runs/20261020T093355Z-dmtcp-c-single-7bce` |

**Interpretation.**

- **The numbers fit the trial design.** The checkpoint was taken shortly after launch (about 40 ticks of 10 ms). The workload then ran on for about 4 s, which gives 450 at termination.
- **The restored process continued from checkpointed memory.** It reported the original nonce and resumed at 40, inside the checkpoint window, rather than at 0 or at 450. It then advanced by about 300 ticks during the 3 s observation.
- **Memory content was preserved.** The verifier also requires valid digests and the intact 4 MiB nonce-derived ballast. The trial verdict PASS implies those checks passed.
- **This is the first real evidence that DMTCP 4.2.0 checkpoints and restores a running unmodified process with its in-memory state on Ubuntu 24.04 ARM64.** It covers **same boot, one class (`c-single`), one trial.**

| Scope | Status |
|---|---|
| Same-boot checkpoint/restore, DMTCP, `c-single` | **VERIFIED PASS** (1 trial) |
| Cross-reboot restoration | **NOT TESTED** |
| Other workload classes (`c-threads`, `py`, `c-file-static`, `c-child`, `c-file-grow`, `tcp`, `gui`) | **NOT VERIFIED** |
| CRIU | **NOT RUN** (privileges, OD-14) |
| Production coordinator security | **NOT APPROVED.** Loopback only, no local authentication; ADR 0004 is Stage 02-only. |
| ADR 0004 status | still **Proposed** in the repository. If the trial ran under your approval, record its acceptance explicitly. |

## U. Stage 02 completion assessment

| Approved criterion | Source | Status |
|---|---|---|
| Real checkpoint → terminate → restore, with proven state continuity | roadmap Stage 02 gate | **MET** (same boot, `c-single`) |
| Cross-reboot restore verified before full acceptance | Stage 02 brief and blueprint stage plan | **MET** for `c-single` (section W) |
| CMP-001: matrix over 7 representative classes | requirements | **OPEN:** 1/7 run. `tcp` and `gui` are deliberately blocked (I-1c, EXC-001), so the matrix needs recorded "blocked by design" entries for them. |
| LIM-003: TCP behaviour measured | requirements | **DECIDED: LIMITED/BLOCKED finding.** TCP restore behaviour was **not measured**, because a TCP workload would make DMTCP 4.2.0 open an `INADDR_ANY` restore listener (I-1c). Socket-holding workloads are unsupported under DMTCP until a safe method exists. This is a recorded limitation, **not** feature support. |
| LIM-007: file changed after checkpoint | requirements | **OPEN** (`c-file-static`, `c-file-grow` not run) |
| PRF-001 to PRF-003: durations and sizes recorded per trial | requirements | **PARTIAL:** recorded for one trial (1.21 s, 25 MB). Restore duration is in `result.json`, not yet reported. |
| PLT-009: engine self-check recorded | requirements | **DECIDED: LIMITED finding.** DMTCP 4.2.0 has no engine self-check command. The recorded substitute is `dmtcp_launch --version` (pinned 4.2.0) plus a passing `dmtcp-isolation-check`. This checks version and network confinement, **not** kernel feature support. |
| Backend decision (ADR, OD-01) | blueprint deliverable | **DRAFTED:** [ADR 0005](../decisions/0005-checkpoint-backend-selection.md) (Proposed). DMTCP 4.2.0 is provisionally selected; CRIU is the alternative; DMTCP is not production-safe. |
| ADR 0004 acceptance recorded | ADR 0004 | **RECORDED:** accepted (limited) for the executed same-boot diagnostic. Amendment 1 (cross-reboot) is proposed. |

**Smallest set to close Stage 02:** a cross-reboot `c-single` trial; a backend-decision ADR; the matrix filled at least for `c-threads`, `py`, and `c-file-static`, with `c-child`, `c-file-grow`, `tcp`, and `gui` recorded as blocked or limited; and your decisions on LIM-003 and PLT-009.

### Cross-reboot verification proposal (not enabled; needs security review and approval)

Smallest safe change, an **ADR 0004 amendment**:

1. **Allow the flag on the two-phase reboot test.** `--adr-0004-controlled-trial` would also be accepted on `reboot-prepare` and `reboot-resume`, for `dmtcp` + `c-single` only. Every ADR 0004 condition applies to both phases. This is now implemented (section V), but has not yet been run.
2. **`reboot-prepare`** (pre-reboot boot) does the following:
   - runs the existing launch, the precheck (one process, no sockets), and the verified checkpoint wait;
   - terminates the original and confirms the image is on ext4;
   - writes the manifest (artifact SHA-256s, workload binary hash, boot ID, DMTCP version, ADR flag) and `sync`s.

   Every coordinator is stopped before reboot, so nothing listens across the reboot.
3. **Manual step.** You run `sudo systemctl poweroff`, then start the VM from UTM. Nothing reboots automatically.
4. **`reboot-resume`** must do the following:
   - require that the boot ID changed, the artifact and binary hashes are unchanged, and DMTCP is still 4.2.0;
   - **take a fresh listener baseline.** The current `reboot_resume` path does not, so the network guard would refuse to run: fail-closed, but it must be fixed;
   - run a fresh isolation check in the new boot;
   - restore through a new loopback hand-off coordinator, with the same audits, continuity rules (pre-reboot nonce, counter in `[ckpt_lower, kill)`), and leak scan.
5. **Code needed** (estimate: about 30 lines plus tests):
   - extend `controlled_trial_refusal()` to accept the two reboot entry points;
   - store and restore the flag in the manifest;
   - add the baseline to `reboot_resume`;
   - add the CLI flag to both subcommands.

   No other gate changes.

Risk: none beyond ADR 0004, plus a DMTCP restore after reboot depending on absolute paths (binary, report file) that live on persistent ext4 under the run directory.

## V. Cross-reboot preparation (ADR 0004 Amendment 1): implemented, NOT YET RUN

Implementation:

| File | Change |
|---|---|
| `ngexp/trial.py` | `controlled_trial_refusal()` now also accepts `reboot-prepare` and `reboot-resume` for `dmtcp`/`c-single` only. `matrix` and everything else are still refused. |
| `ngexp/trial.py` | `reboot_prepare` writes the manifest **only after** cleanup passed and every coordinator is confirmed stopped |
| `ngexp/trial.py` | `resume_preconditions()` (pure function) holds the resume gates |
| `ngexp/trial.py` | `reboot_resume` adds the engine check, a fresh baseline, a fresh isolation check, and the single-resume marker |
| `ngexp/runner.py` | `host_identity()` returns the architecture and a SHA-256 of `/etc/machine-id` |
| `ngexp/cli.py` | `--adr-0004-controlled-trial` on `reboot-prepare` and `reboot-resume`; `reboot-resume --backend/--class` (must match the manifest); `--run-dir` optional (newest run not yet resumed) |
| `ubuntu-controlled-trial.sh` | `--reboot-prepare` and `--reboot-resume` modes. All prerequisites (SHA, clean tree, baseline, Linux security tests with zero skipped, fresh isolation check) rerun in each boot. The script never reboots. |

Regression tests (`tests/unit/test_stage02_reboot.py`, fake backend, any host). Each of these stops the resume **before any restore**:

- unchanged boot ID;
- missing checkpoint;
- corrupt checkpoint (one flipped byte);
- wrong DMTCP version;
- failed fresh isolation;
- scope or flag mismatch;
- already resumed;
- missing manifest.

Listener exposure during restore aborts the resume. A failed restoration never produces a continuity PASS. A cleanup failure (leaked process) fails the resume. Process identity mismatch (a second process in the group, or a workload socket) stops the checkpoint. `reboot-prepare` writes no manifest if cleanup fails or a coordinator is still alive.

### Ubuntu procedure (stop for review before the first cross-reboot test)

```sh
cd ~/napguard && git fetch origin && git checkout stage-02/checkpoint-feasibility && git pull --ff-only && git rev-parse HEAD
# Boot 1: prerequisites + prepare (no reboot happens here)
bash experiments/stage02/ubuntu-controlled-trial.sh <SHA> --reboot-prepare 2>&1 | tee ~/s02-reboot-prepare.log; echo "EXIT=${PIPESTATUS[0]}"
# Only if EXIT=0: YOU power off and start the VM again (UTM). Then, in boot 2:
cd ~/napguard && bash experiments/stage02/ubuntu-controlled-trial.sh <SHA> --reboot-resume 2>&1 | tee ~/s02-reboot-resume.log; echo "EXIT=${PIPESTATUS[0]}"
```

**Expected evidence.**

- **Prepare:** `controlled-trial preconditions`, `native checkpoint created`, `original terminated`, `all coordinators stopped before reboot`, `reboot manifest written and synced`, and `experiment processes cleaned up`, all PASS.
- **Resume:** `system rebooted since checkpoint` (a different boot ID), `checkpoint artifacts intact after reboot`, `same backend build as before reboot`, every `post-reboot isolation: …` check, `restore command started`, `restored state continuity (main)` (pre-reboot nonce, counter in the pre-reboot window), and `experiment processes cleaned up`, all PASS.
- **Network:** no new non-loopback listeners after either boot.

**Status: run once on Ubuntu, VERIFIED PASS. See section W.**

## W. Cross-reboot checkpoint/restore: VERIFIED PASS (DMTCP 4.2.0, c-single)

These are user-reported results from 2026-10-20, from a run on Ubuntu 24.04.5 ARM64 at commit `55905be43fcf67ad1effd2dd5d17e5d1b6c57867` under ADR 0004 Amendment 1. Claude has not reviewed the raw evidence files.

| Phase | Observed |
|---|---|
| Prepare (boot 1) | **PASS**, `PREPARE_EXIT=0`. Checkpoint image 25,050,848 bytes. Verified checkpoint completion 1.23 s. Original terminated. All coordinators stopped before reboot. Manifest persisted and synced. |
| Boot IDs | before `6ee8d6d4-3c1c-4b0c-a3ef-baf04ba89a4c`, after `ed8b9cf5-5112-4051-866e-9ef5f5d42afe` (a real reboot) |
| Resume (boot 2) | **PASS**, `RESUME_EXIT=0` |
| Integrity | all 3 persistent artifacts matched their pre-reboot SHA-256; backend version and tool hashes unchanged |
| Isolation | fresh post-reboot coordinator isolation PASS; no new non-loopback listeners |
| State continuity | original nonce `389daf1268d15b31f429f7688645ce90` preserved. Counter 430 at termination, **40 after restore** (rolled back into the checkpoint window, not 0), then advanced to 340. |
| Cleanup | PASS; no leftover DMTCP or workload processes |
| Evidence (VM) | `~/.local/share/napguard-stage02/runs/20261020T100428Z-reboot-dmtcp-c-single-ed7a` |

**Finding.** This is genuine **cross-reboot restoration of execution state**. A process image checkpointed before a full VM power cycle was restored after it, from persistent ext4, and continued from checkpointed memory. **It is limited to the tested `c-single` workload** (one trial). No restore duration is stated here; see section X for how to extract one.

## X. Evidence matrix, closure table, and approvals

### Evidence matrix

States: VERIFIED PASS, VERIFIED FAIL, BLOCKED (with documented reason), NOT TESTED.

| Item | Requirement or decision text (short) | State | Evidence or reason |
|---|---|---|---|
| Same-boot genuine checkpoint/restore | roadmap Stage 02 gate | **VERIFIED PASS** (c-single) | section T |
| Cross-reboot genuine checkpoint/restore | Stage 02 brief | **VERIFIED PASS** (c-single) | section W |
| PLT-009 | backend self-check recorded at startup | **VERIFIED PASS (substitute check)** | DMTCP has no self-check command. The substitute is `dmtcp_launch --version` = 4.2.0 plus `dmtcp-isolation-check` PASS (sections O, T, W). It covers version and network confinement, not kernel features. **The owner must accept the substitute.** |
| PRF-001 | checkpoint duration recorded | **VERIFIED PASS** (2 observations) | 1.206 s (same boot, from `summarize`) and about 1.23 s (cross-reboot, from the preparation log). See section Y. |
| PRF-002 | image size recorded | **VERIFIED PASS** (2 observations) | 25,059,040 B (same boot, from `summarize`) and 25,050,848 B (cross-reboot, from the preparation log). See section Y. |
| PRF-003 | restore duration recorded | **VERIFIED PASS (upper bound)** (2 observations) | 1.217 s (same boot) and 1.218 s (cross-reboot), both from `summarize`. Correct definition: the time from the restore command starting until the harness had seen **at least 3** restored reports per role, adopted them, and audited listeners. That is an **upper bound** on restore-to-first-report, not the first-report time itself. Precise first-report latency is recorded from now on. See section Y. |
| CMP-001 `c-single` | compatibility matrix | **VERIFIED PASS** | sections T, W |
| CMP-001 `c-threads`, `py`, `c-file-static` | compatibility matrix | **NOT TESTED** | authorised by ADR 0004 Amendment 2 (approved 2026-10-20); awaiting the Ubuntu run |
| CMP-001 `c-file-grow` | compatibility matrix | **NOT TESTED** | Amendment 2 approved; awaiting the Ubuntu run; it doubles as the LIM-007 probe |
| CMP-001 `c-child` | compatibility matrix | **BLOCKED** | multi-process; the single-process precheck refuses it, and multi-process restore and cleanup need a separate review |
| CMP-001 `tcp` | compatibility matrix | **BLOCKED** | DMTCP 4.2.0 opens an `INADDR_ANY` restore listener for workload sockets (I-1c) |
| CMP-001 `gui` | compatibility matrix | **BLOCKED** | X11/Wayland sockets (I-1c) and out of scope (EXC-001); not implemented |
| LIM-003 | TCP behaviour reported | **BLOCKED** | recorded as a limited finding: not measured, because measuring it safely is impossible under I-1c |
| LIM-007 | changed open files after checkpoint | **NOT TESTED** | `c-file-static` / `c-file-grow` await Amendment 2. The held-file consistency checks are implemented. |
| OD-01 backend selection | blueprint deliverable | **BLOCKED (awaiting owner approval)** | [ADR 0005](../decisions/0005-checkpoint-backend-selection.md) drafted (Proposed): DMTCP provisional, CRIU the alternative, not production-safe |

### Closure table

| Mandatory exit criterion | Evidence | Verdict | Required action |
|---|---|---|---|
| Real checkpoint → terminate → restore with proven state continuity | sections T, W | **MET** | none |
| Cross-reboot restore before full acceptance | section W | **MET** | none |
| Backend decision recorded | ADR 0005 | open | owner accepts ADR 0005 |
| CMP-001 compatibility matrix | matrix above | **partial** | either approve Amendment 2 and run 4 classes, **or** accept `c-single`-only coverage with the rest recorded NOT TESTED or BLOCKED |
| LIM-003 TCP | I-1c | BLOCKED | owner accepts it as a limitation |
| LIM-007 changed files | none yet | NOT TESTED | Amendment 2 (`c-file-static`, `c-file-grow`) **or** owner accepts NOT TESTED |
| PLT-009 self-check | version + isolation | PASS (substitute) | owner accepts the substitute |
| PRF-001, PRF-002 | 2 trials | PASS | none |
| PRF-003 | on the VM only | NOT TESTED | run the read-only `summarize` and report the value |

**Feasibility is demonstrated. Stage 02 is not formally complete** until the owner either accepts the open items above as limitations, or approves and runs Amendment 2.

### Approvals needed before each further experiment

| Experiment | Needs |
|---|---|
| `summarize` (read-only) | nothing (reads `result.json` only) |
| Linux-only test suites (`pytest`) | nothing (native processes only, no DMTCP coordinator) |
| `dmtcp-isolation-check` | nothing new (already approved; fail-closed) |
| `c-threads`, `py`, `c-file-static`, `c-file-grow` same-boot trials | **ADR 0004 Amendment 2 approval**, then a one-line change to `CONTROLLED_TRIAL_CLASSES` plus a `--class` option in `ubuntu-controlled-trial.sh` |
| Any of those across a reboot | a further amendment (not proposed) |
| `c-child`, `tcp`, `gui`, CRIU | not proposed; separate design and security review (I-1c, OD-14) |

### Safe test preparation implemented (no new experiment run)

- **Held-file consistency** (`verify.verify_held_file`). For `c-file-static`, the held file must be byte-identical at checkpoint, at termination, and after restore; this check is mandatory. For `c-file-grow`, the behaviour (append, rewind, or overwrite with torn lines) is recorded as a LIM-007 observation and does not decide the class verdict; state continuity does.
- **Snapshots** of the held file at checkpoint completion, at termination, and after restore are stored in `result.json`.
- **`run_stage02.py summarize`**: read-only extraction of checkpoint seconds, image bytes, `restore_to_first_report_s`, and continuity counters from every `result.json`. Missing values print as `-`, never as estimates.
- **Tests** (`tests/unit/test_stage02_classes.py`):
  - the four classes are still refused for `trial` and both reboot entries;
  - held-file verifier cases;
  - the summary reports only recorded values and writes nothing;
  - **Linux-only:** each Amendment 2 class, run natively without DMTCP, is a single process with no workload sockets and valid digests. The static file stays unchanged and the growing file grows. `c-child` would be refused by the single-process precheck.

## Y. Recorded measurements (individual observations, not benchmark averages)

| Run | Phase | Checkpoint completion | Image | Restore timing | Counters (termination → first restored → last) | Verdict | Source |
|---|---|---|---|---|---|---|---|
| `20261020T093355Z-dmtcp-c-single-7bce` | same boot | 1.206 s | 25,059,040 B | 1.217 s (upper bound, see below) | 450 → 40 → 340 | PASS | `summarize` (`result.json`) |
| `20261020T100428Z-reboot-dmtcp-c-single-ed7a` | prepare (boot 1) | about 1.23 s | 25,050,848 B | — | — | PASS | **preparation log** |
| `20261020T100428Z-reboot-dmtcp-c-single-ed7a` | resume (boot 2) | — | — | 1.218 s (upper bound) | 430 → 40 → 340 | PASS | `summarize` (`result.json`) |

**Why the cross-reboot checkpoint values come from the log.** Prepare and resume share one run directory, and at commit `55905be4` the resume's `result.json` **overwrote** the prepare's. The prepare-phase checkpoint facts survived only in its log. This commit fixes that: each phase also writes `result-<kind>.json`, and `summarize` reads those files.

**Restore timing definition (PRF-003).** Two values are recorded from this commit on:

- `restore_to_first_report_s`, which is **precise**: the `mono_ns` of the earliest restored `NGSTATE` report minus `time.monotonic_ns()` taken immediately before the restore command. Both use `CLOCK_MONOTONIC` in the same boot. If the clocks do not line up, it is `None`, never a guess.
- `restore_to_three_reports_upper_bound_s`: the previous measurement, wall time from restore start until at least 3 restored reports per role were seen, adopted, and audited.

Runs from before this commit stored the upper bound under the name `restore_to_first_report_s`. `summarize` now reports those values as `restore_upper_bound_s` with `restore_timing = legacy upper bound`.

## Z. Compatibility trial preparation (ADR 0004 Amendment 2): APPROVED 2026-10-20, NOT YET RUN

- **The gate** is `backends.AMENDMENT_2_CLASSES = (c-threads, py, c-file-static, c-file-grow)`, guarded by `AMENDMENT_2_APPROVED = False` and read at call time from that single source. When approved, those classes are allowed for **same-boot `trial` only**: never `matrix`, never `reboot-*`. `NEVER_UNDER_ADR_0004 = (c-child, tcp, gui)` is refused regardless of approval.
- **The Ubuntu script** takes `--class` with a strict allowlist (`c-single | c-threads | py | c-file-static | c-file-grow`). Anything else is a usage error (exit 2), including `c-child`, `tcp`, `gui`, a wrong case, and shell metacharacters. Reboot modes accept `c-single` only. A harness refusal (exit 2) is reported as **BLOCKED**, so until approval a run like `--run-trial --class py` reports BLOCKED without checkpointing anything.
- **Checks per class** (unchanged, all mandatory except where noted):
  - one process, no workload sockets;
  - fresh isolation and the fail-closed hand-off;
  - verified checkpoint completion;
  - original termination;
  - per-role state continuity (`c-threads`: every one of `main`, `t0`–`t2`);
  - held-file consistency: mandatory for `c-file-static`, a recorded LIM-007 observation for `c-file-grow`;
  - listener audits and the leak scan.
- **New tests** (`tests/unit/test_stage02_classes.py`):
  - the flag is pinned off;
  - the approved path stays same-boot trial only, and the never-allowed classes stay refused;
  - multi-role continuity (one non-rolled-back thread fails);
  - precise and legacy timing;
  - the script allowlist (9 rejection cases) and BLOCKED mapping;
  - a `restore_and_verify` integration test for `c-file-static` with a fake backend: file consistency PASS, a changed file FAIL, and network exposure aborting before any continuity claim;
  - per-phase result preservation.

**Status:** the project owner **approved** ADR 0004 Amendment 2 on 2026-10-20 (one same-boot trial per class). `AMENDMENT_2_APPROVED = True` and the pinned tests were updated in the commit after `22175d3a`. **No Amendment 2 class has been run yet; none is claimed to pass.** The base exception (`c-single`), the global gate (`False`), the DMTCP 4.2.0 pin, and the prohibitions on `c-child`, `tcp`, `gui`, and cross-reboot runs of these classes are unchanged. The Ubuntu script's security-test step now runs all Stage 02 test modules (safety, launch config, classes, reboot, ckptwait), with zero skips allowed.

## AA. Amendment 2 results so far, the `py` restore failure, and Amendment 3 (APPROVED 2026-10-20, NOT YET RUN)

This section supersedes the `c-threads` and `py` rows of the section X matrix. Those rows are kept unchanged as history.

**Amendment 2 trials at `52e5baed59af16b34ccd6ed47cf4037d32e70f07`** (Ubuntu 24.04.5 ARM64, Python 3.12.3, DMTCP 4.2.0; user-reported):

| Class | Verdict | Evidence |
|---|---|---|
| `c-threads` | **PASS** (same boot) | user-reported; figures not yet transcribed here |
| `py` | **FAIL** (restore) | checkpoint verified: 43,099,292 bytes in 1.19 s; original terminated; then `restore command started` FAIL: `dmtcp_restart exited within 1 s` |
| `c-file-static`, `c-file-grow` | NOT TESTED | awaiting their Amendment 2 runs |

**This `py` failure is a recorded Stage 02 FAIL.** It stays recorded whatever any retest shows.

**Analysis (Mac, read-only; root cause NOT established):**

- **The `setsid()` warning is incidental.** The restart log contained `JWARNING(setsid() != -1) failed … Operation not permitted … Failed to restore this process as session leader.` In DMTCP 4.2.0 this is a `JWARNING` in `RestoreTarget::createProcess` (`dmtcprestartinternal.cpp:323-329`): it logs and continues to `runMtcpRestart`.
  - The harness spawns `dmtcp_restart` with `start_new_session=True`, so the restarter already leads its own session and `setsid()` must fail with EPERM, for every class.
  - The owner confirmed the same warning in the passing C workloads.
- **Launch and restore paths are identical** for C and Python: same own-session spawn, same restart argv, same loopback hand-off coordinator. They differ in the program (`/usr/bin/python3` versus `ngcounter`) and in image size (43 MB versus about 25 MB).
- **The restarter becomes the restored process** (`dmtcp_restart` execs `mtcp_restart` in place). An early exit therefore means one of three things: the restarter failed, the image crashed during restore, or the restored interpreter ran and exited.
- **Missing evidence (now captured, commit `7d3b4c9090e313cca7f3a06faf40b992cdea47b1`).** The failed check kept only keyword-filtered log lines. These surfaced the warning, which matches "error" via `strerror`, and could hide the fatal line. They also omitted the exit code or signal.
- **Owner's read-only Ubuntu diagnostics:** no kernel events in the failure window; about 2.7 GiB available RAM; about 3.8 GiB available swap; overcommit mode 0; the checkpoint artifacts preserved.

**Amendment 3 (ADR 0004): one-time owner approval, 2026-10-20.** It allows exactly one additional same-boot `py` diagnostic retest. Every ADR 0004 and Amendment 2 condition is unchanged. The harness enforces it:

- `backends.ONE_TIME_RETESTS = {"py": 1}`. A controlled `py` trial is refused (BLOCKED) unless exactly one earlier `py` run that launched a workload, the preserved original, exists in the run root.
- After the retest has launched, every further `py` trial is refused. Refused or BLOCKED runs that never launched a workload are not counted.
- The result records `one_time_retest.earlier_launched_runs`.
- `reboot-*`, `matrix`, `c-child`, `tcp` and `gui` stay refused.
- **Diagnostics on any restore outcome:**
  - `restore command started` FAIL facts: `restarter_exit` (exit code, or signal plus core flag, read with `waitid(WNOWAIT)`, so nothing is reaped), `restarter_stderr_tail` (60 lines, unfiltered), `restarter_stdout_tail`, and `restore_coordinator_tail`;
  - `reports_after_kill_at_restore_fail`;
  - `restore_process_diagnostics` after the restore has started, including a restarter that dies later than the 1 s start check.
- **Tests:** `tests/unit/test_stage02_restore_diagnostics.py` (18 tests). The Ubuntu script now includes it in its zero-skip security-test step.

**Unchanged:** `DMTCP_LOOPBACK_ONLY_COORDINATOR_VERIFIED = False`, `CONTROLLED_TRIAL_CLASSES = ("c-single",)`, the fd-821 loopback-only hand-off, the DMTCP 4.2.0 pin, the Python workload, and the success path for `c-single` and `c-threads` (only diagnostic facts were added).

**Status:** approved, **not yet run**. No `py` restore is claimed to work.
