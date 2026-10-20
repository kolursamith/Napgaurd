# Stage 02 — Checkpoint Engine Feasibility Experiments

This is experiment code for Ubuntu 24.04 aarch64 only. It is not NapGuard's production checkpoint backend, which is Stage 03. The design, research, and gate are in [`docs/phases/STAGE-02-report.md`](../../docs/phases/STAGE-02-report.md).

## Contents

| Path | Purpose |
|---|---|
| `workload/ngcounter.c` | Unmodified, checkpoint-unaware C workload. Its state is in memory only. Classes: single, threads, child process, held file. |
| `workload/ngcounter.py` | The same state model in Python (the CMP-001 Python class) |
| `ngexp/model.py` | The state definition shared with the verifier (splitmix64 digest chain, ballast) |
| `ngexp/verify.py` | Rules that decide "resumed from checkpointed memory" versus "fresh start" |
| `ngexp/runner.py` | Command recording with timeouts, process identity, cleanup, environment probes |
| `ngexp/backends.py` | DMTCP and CRIU drivers |
| `ngexp/trial.py` | Trial, matrix, and reboot-prepare/resume orchestration |
| `run_stage02.py` | Command line |
| `build-dmtcp.sh`, `build-criu.sh` | Source builds from pinned upstream commits, without sudo |
| `ubuntu-controlled-trial.sh` | Ordered prerequisite checks plus, only with an explicit mode flag, one ADR 0004 c-single DMTCP run: `--run-trial` (same boot), `--reboot-prepare`, or `--reboot-resume` (Amendment 1). `--class` is strictly allowlisted; non-`c-single` classes need Amendment 2 (disabled) and report BLOCKED until then. |

## How a trial proves real restoration

1. **Fresh-start control.** The workload is launched normally. A fresh start always has a new random 128-bit nonce, and its counter begins at 0.
2. **Launch under the backend.** NapGuard records the nonce and the counters.
3. **Real native checkpoint.** NapGuard records the counters observed just before the request (`ckpt_lower`) and just after the checkpoint completes (`ckpt_upper`). The workload must keep running (FR-011).
4. **Run on, then terminate.** The workload runs for 4 s more, then every original process is killed and confirmed dead. NapGuard records the counters at termination (`kill`).
5. **Restore with the backend's own command.**
6. **PASS requires all of the following:**
   - the same nonce;
   - a first restored counter in `[ckpt_lower, kill)`, which means it rolled back to the checkpoint instead of starting at 0 or continuing;
   - digests consistent with the nonce and counter;
   - an intact nonce-derived ballast buffer;
   - continued progress after the restore.

The workloads append observations with open/append/close and never read the report file or any other saved state. The harness reads the report file only to observe the state.

## Commands (inside Ubuntu, from the repository root)

```sh
python3 -I experiments/stage02/run_stage02.py env                          # environment + backend availability
python3 -I experiments/stage02/run_stage02.py trial --backend dmtcp --class c-single
python3 -I experiments/stage02/run_stage02.py matrix --backend dmtcp       # all classes
python3 -I experiments/stage02/run_stage02.py trial --backend criu --class c-single
python3 -I experiments/stage02/run_stage02.py reboot-prepare --backend dmtcp
python3 -I experiments/stage02/run_stage02.py reboot-resume --run-dir <printed run dir>
python3 -I experiments/stage02/run_stage02.py dmtcp-isolation-check     # OD-13 evidence only
```

`-I` (isolated mode) is intended: PYTHONPATH, the user site directory, and the current directory cannot inject modules. The launcher adds back only its own directory. DMTCP `trial`, `matrix`, and `reboot-*` currently report BLOCKED (see Safety below).

Exit status: 0 PASS, 1 FAIL, 2 BLOCKED, 3 SKIP. A missing backend is BLOCKED, never PASS.

Evidence for each run is kept in `~/.local/share/napguard-stage02/runs/<run-id>/`, which is on the persistent ext4 root filesystem:

- `result.json`
- `commands.jsonl` and `events.jsonl`
- `logs/`
- `reports/`
- `checkpoint/`

Nothing outside that directory, `~/.cache/napguard-stage02`, or `~/.local/opt/napguard-stage02` is written.

## Safety

- No sudo is used unless you pass `--criu-mode sudo` explicitly; that mode is diagnostic only.
- No sysctl or other global system changes are made.
- Every command has a timeout and runs in its own session.
- Only processes the harness created, or adopted by verified identity, are ever signalled. Adoption requires the process to be owned by the user, in a session this run created, and started after the launch or restore command. Groups are signalled only while their leader is the harness's own unreaped child, and adopted processes are signalled through a pidfd.
- **DMTCP trials never start a coordinator.** DMTCP 4.2.0 coordinators listen on all interfaces without authentication, and no supported option restricts them, so DMTCP trials report BLOCKED (report I-1a, OD-13). `build-dmtcp.sh` refuses `make check` for the same reason.
- **The one exception is `dmtcp-isolation-check`**, which starts a single coordinator using DMTCP's internal listener hand-off: a 127.0.0.1 listener pre-bound by the harness, passed on fd 821, with the same `--coord-port`. It is fail-closed: a fallback wildcard bind is refused by the kernel. It then proves the coordinator is unreachable from the VM's other addresses and stops it, without running a workload or taking a checkpoint. A PASS does not open the trial gate; that needs an ADR and approval (report section N).
- **Trial network guard.** After launch, checkpoint, and restore, any non-loopback listener owned by an experiment process aborts the trial.
- CRIU dumps only the PID the harness itself launched.
