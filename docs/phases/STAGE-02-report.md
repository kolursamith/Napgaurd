# Stage 02 Report: Real Checkpoint Engine Feasibility

- Date: 2026-10-19
- Baseline: Stage 01 PASS (user-reported) at `39714383fcd9b683f367e7476882bfb7102adaa3` on `stage-01/requirements-design`
- Branch: `stage-02/checkpoint-feasibility`, created from the approved Stage 01 commit. The commit SHA is recorded in Git history and the Ubuntu logs.
- Verdict: **BLOCKED pending a security decision (OD-13 or OD-14).** Preparation is complete, but neither backend can run a trial under the current safety rules:
  - The harness refuses to start a DMTCP coordinator, because no supported option keeps it off the network (section I-1a).
  - CRIU needs privileges as an ordinary user (I-2).
  - No checkpoint has run on Ubuntu yet.

Stage 02 PASS needs Ubuntu ARM64 evidence that a backend checkpoints and restores a real running process with its execution state preserved, in the same boot and across a VM reboot. Nothing in this report is that evidence. No checkpoint experiment was run on macOS.

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

No checkpoint or restore has run on Ubuntu. Neither backend is installed.

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
| Same-boot restore on VM | **NOT RUN** | **NOT RUN** |
| Cross-reboot restore on VM | **NOT RUN** | **NOT RUN** |

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
| A backend installs on Ubuntu 24.04 aarch64 | build logs | NOT RUN |
| Genuine checkpoint and restore with preserved in-memory state (same boot) | `trial` `result.json` PASS | NOT RUN |
| Cross-reboot restore | `reboot-resume` `result.json` PASS | NOT RUN |

| No DMTCP coordinator exposed on the network | harness refusal + `test_stage02_safety.py` | ENFORCED (refuses to start) |

**Stage 02: BLOCKED pending OD-13 (DMTCP coordinator exposure: hand-off mechanism implemented, not yet verified on Ubuntu) or OD-14 (CRIU privileges).** The preparation is complete and reviewed. The only steps that can run now are `env`, the builds, and the CRIU-as-user trial, which is expected to report BLOCKED. STOP: Stage 03 has not been started.

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
