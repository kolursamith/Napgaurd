# Phase 10 Report — Final Stabilization and Release Gate

Date: 2026-10-24

## Outcome

The reported nine-error Ubuntu cluster has one shared test-collection root cause. The
module `tests/unit/test_final_history.py` defined an ordinary helper named `setup` and
contains exactly nine tests. The Ubuntu system test runner treated that name as a legacy
nose setup hook, invoked it with a test function, and line 23 then attempted to evaluate
`test_function / "data"`, raising `TypeError` during setup for all nine tests. Pytest 9.1.1
on the Mac has removed that compatibility behavior, which is why the same nine tests pass
locally.

The helper is now named `make_manager`. A new repository-wide AST regression test rejects
module-level plain `setup` or `teardown` hooks in `test_*.py`, so a newer pytest runner
cannot conceal another occurrence. The final Ubuntu verifier now captures its actual
pytest version before running the mandatory Linux suites.

The interrupted Mac failures were independently diagnosed. In the restricted command
sandbox, the first daemon test failed at `socket.bind(AF_UNIX)` with
`PermissionError: [Errno 1] Operation not permitted`; `ps` was also denied. The identical
IPC test passed outside that sandbox, and the complete suite then passed outside the
sandbox. No production IPC or process-management workaround was made for a test-runner
restriction.

This work is ready for Ubuntu verification, but it is not Ubuntu-verified. The complete
Ubuntu evidence directory named in the request was not available on this host, and no
Linux, DMTCP, X11/Tk, systemd, `/proc`, `ss`, or `SO_PEERCRED` acceptance claim is made.

## Changes

- Renamed the ambiguous history-test factory from `setup` to `make_manager` and updated
  every caller.
- Added `tests/unit/test_pytest_hygiene.py`, which detects legacy nose hook names across
  all test modules.
- Made `scripts/final-ubuntu-check.sh` save and display `pytest --version`, and block with
  an evidence-file reference if the system Python cannot run pytest.
- Corrected the implementation-status GUI inventory from eight to nine screens.
- Recorded the Mac test count, the Ubuntu failure diagnosis, and the still-pending Linux
  verification boundary in `docs/IMPLEMENTATION_STATUS.md`.
- No checkpoint, restore, security, daemon, IPC, GUI, catalog, cleanup, or DMTCP production
  behavior was weakened or replaced with a mock.

## Failure and component audit

| Component | Evidence/current behavior | Problem/root cause | Change or disposition | Proving check |
|---|---|---|---|---|
| Ubuntu history tests | Report: `tests/unit/test_final_history.py:23: TypeError`, 208 passed and 9 errors | Plain module helper `setup` was collected as a legacy nose hook; nine tests produced nine setup errors | Rename to `make_manager`; add suite-wide reserved-name regression | Focused module: 9 passed; hygiene: 1 passed; Ubuntu rerun pending |
| Daemon/IPC tests on Mac | Restricted run failed at `src/napguard/daemon/ipc.py:129`, `sock.bind`, `EPERM` | Command sandbox forbids Unix-domain socket creation; it also forbids `ps` | Run socket/process tests in the permitted environment; no product-code change | Identical IPC test: 1 passed; final permitted suite: 434 passed |
| Catalog/history | Migration, archive, safe preview/delete, interrupted operation, symlink refusal | No additional local defect reproduced | Existing behavior preserved | `test_final_history.py`: 9 passed |
| Packaging/daemon lifecycle | Sandbox HOME install/uninstall/purge, single data-directory lock, shutdown helpers | No additional local defect reproduced | Existing behavior preserved | Final modules: 39 passed, 1 display-only skip |
| GUI worker/presenter | Queue-based main-thread delivery, action eligibility, evidence formatting | Real Tk/display unavailable on Mac | Existing implementation preserved; corrected documentation count | Worker/presenter tests passed; GUI smoke NOT RUN |
| Scheduler/simulation | Test-double integration submits serialized real-operation requests and labels simulation | Genuine DMTCP result cannot be established on Mac | Existing implementation preserved | Portable tests passed; Ubuntu end-to-end NOT RUN |
| Final Ubuntu verifier | Exact SHA/clean tree, isolated data/socket, baseline/static checks, GUI, genuine DMTCP workflow, cleanup/leak checks | Test log did not state which system pytest collected the failing suite | Record `pytest-version.txt` before mandatory Linux tests | `bash -n` PASS; macOS fail-closed exit 2; Ubuntu run NOT RUN |

## Commands and results

### Repository and environment

| Command | Result |
|---|---|
| `pwd; uname -a; sw_vers; arch; id` | macOS 14.8.1, Darwin 23.6.0, arm64, ordinary user UID 501 |
| `git status --short --branch; git remote -v; git log -5 --oneline` | Clean start on `stage-03/backend-integration`; `origin` is `https://github.com/kolursamith/Napgaurd.git`; HEAD `b5ae58f` |
| `git fetch origin stage-03/backend-integration` | PASS |
| `git rev-list --left-right --count HEAD...origin/stage-03/backend-integration` | `0 0` at phase start |
| `.venv/bin/python --version` | Python 3.14.7 (Mac diagnostic environment) |
| `.venv/bin/pytest --version` | pytest 9.1.1 |
| `.venv/bin/ruff --version` | Ruff 0.17.0 |
| `.venv/bin/mypy --version` | mypy 1.20.2 |

### Failure diagnosis

| Command | Result |
|---|---|
| `.venv/bin/pytest tests/unit/test_final_history.py -vv --tb=long` before the rename | 9 passed locally; established that the Ubuntu-only collection behavior was version-sensitive |
| Restricted `.venv/bin/pytest tests/unit/test_mvp_daemon_ipc.py::test_end_to_end_over_ipc -vv --tb=long -s` | FAIL at `sock.bind(str(self.sock_path))`: `PermissionError: [Errno 1] Operation not permitted` |
| The identical IPC command in the permitted environment | PASS, 1 passed in 1.56 s |

### After the fix

| Command | Result |
|---|---|
| `.venv/bin/pytest tests/unit/test_final_history.py tests/unit/test_pytest_hygiene.py -q --tb=long` | PASS, 10 passed |
| `.venv/bin/ruff check tests/unit/test_final_history.py tests/unit/test_pytest_hygiene.py` | PASS |
| `.venv/bin/mypy tests/unit/test_final_history.py tests/unit/test_pytest_hygiene.py` | PASS, no issues in 2 files |
| `.venv/bin/pytest -ra --tb=long` in the permitted environment | Final PASS, **434 passed, 33 skipped**, 0 failed in 82.62 s; all skips explicitly require Linux `/proc`, Linux/`ss`, `SO_PEERCRED`, or Tk plus a display |
| Final packaging/IPC/GUI-logic/history modules in the permitted environment | PASS, **39 passed, 1 skipped**; the sole skip requires Tkinter and an Ubuntu desktop display |
| `.venv/bin/ruff check .` | PASS |
| `.venv/bin/mypy src tests experiments/stage02` | PASS, no issues in 79 source files |
| `bash -n` for every shell script under `scripts/` and `experiments/` | PASS |
| `.venv/bin/pytest tests/docs/test_docs_consistency.py -q --tb=long` | PASS, 71 passed |
| `bash scripts/final-ubuntu-check.sh b5ae58f...` on macOS | Correctly refused with `run inside Ubuntu`, exit 2 |
| `git diff --check` | PASS before the report was added; rerun before commit |

## Test classification

- **Unit and portable integration tests:** 434 passed on macOS. Tests using the labelled
  fake backend prove orchestration, validation, failure handling, metadata, and UI
  presenter behavior; they do not prove DMTCP checkpointing.
- **Mac environment skips:** 33. These are not counted as passes and remain Ubuntu work.
- **Real Ubuntu integration/system tests:** NOT RUN for this change.
- **Real DMTCP checkpoint/restore:** NOT RUN for this change.
- **GUI smoke with real display and real daemon:** NOT RUN for this change.
- **Full teacher demo and packaging on Ubuntu:** NOT RUN for this change.

## Known limitations and remaining evidence

1. The full Ubuntu `tests.log` from
   `/home/samith/napguard-final-evidence-20261022T093656Z` was not available locally. The
   available line/count evidence is sufficient to identify and fix the shared nine-test
   hook collision, but the Ubuntu rerun is mandatory confirmation.
2. Only Ubuntu 24.04 ARM64 with pinned DMTCP 4.2.0 can prove genuine launch, checkpoint,
   verified completion, stop, restore, nonce/counter continuity, scheduler-triggered
   checkpoint, and simulated-battery-triggered checkpoint.
3. The Mac has no usable Tk display for the GUI smoke test.
4. Existing documented product limitations remain: reference workloads only,
   developer-only unauthenticated coordinator, newest generation restorable, no
   re-checkpoint of restored instances, no external-state rollback, no automatic cold-boot
   recovery, no physical battery/suspend validation, and no production authorization.

## Git diff summary before commit

- `tests/unit/test_final_history.py`: collision-free helper name.
- `tests/unit/test_pytest_hygiene.py`: new regression check.
- `scripts/final-ubuntu-check.sh`: pytest-version evidence.
- `docs/IMPLEMENTATION_STATUS.md`: corrected screen count and verification status.
- `docs/phases/PHASE-10-plan.md`: approved phase scope and rollback.
- `docs/phases/PHASE-10-report.md`: this evidence report.

No generated logs, caches, checkpoint images, VM artifacts, secrets, or private data are
included.

## Gate table

| Criterion | Command/evidence | Status | Remaining risk |
|---|---|---|---|
| Repository/remote/branch match request | Git inspection plus fetch; local/remote start at `b5ae58f` | PASS | Remote must be rechecked after push |
| Nine-error root cause fixed | Rename plus hygiene test; focused 10-test run | PASS locally | Ubuntu confirmation pending |
| Daemon/IPC failure classified | Restricted `EPERM`; identical permitted test and full suite pass | PASS | macOS is not Linux `SO_PEERCRED` evidence |
| Full portable suite | 434 passed, 33 platform skips, 0 failed | PASS | Skips are mandatory on Ubuntu |
| Ruff | `ruff check .` | PASS | None known |
| Strict mypy | 79 source files | PASS | Python 3.12 runtime still verified on Ubuntu only |
| Shell syntax | Every project shell script | PASS | Runtime semantics remain Ubuntu-only |
| Documentation consistency | 71 passed | PASS | None known |
| Packaging sandbox | Included in final module/full suite | PASS | Desktop install still requires Ubuntu verification |
| Linux `/proc`, `ss`, and `SO_PEERCRED` tests | Requires Ubuntu | NOT RUN | Mandatory; blocks Ubuntu-verified claim |
| Tk GUI smoke against real daemon | Requires Ubuntu desktop display | NOT RUN | Mandatory; blocks presentation-ready claim |
| Genuine DMTCP checkpoint/restore and continuity | Requires pinned DMTCP on Ubuntu ARM64 | NOT RUN | Mandatory; blocks presentation-ready claim |
| Scheduler and simulated battery to genuine checkpoint | Final Ubuntu demo steps 9–10 | NOT RUN | Mandatory; test doubles are not acceptance evidence |
| Install/desktop launcher/full demo/no leftovers | Final Ubuntu verifier `--run-demo` | NOT RUN | Mandatory; blocks presentation-ready claim |

## Phase status

**READY FOR UBUNTU VERIFICATION, not Ubuntu-verified.** The local implementation and
validation gates pass. Mandatory Ubuntu gates are `NOT RUN`, so Phase 10 cannot be marked
fully complete and no later phase is started. STOP pending the exact Ubuntu commands in the
handoff and the returned evidence logs.

---

## Ubuntu GUI-freeze remediation report — 2026-10-24

### Reported failure and root cause

New target evidence at base commit `12e25d67dc2c913010fef6b943acd4e3a4bd88ce`
showed that a genuine `c-single` workload launched successfully and remained alive, but the
GUI stopped responding after the row was selected. Source tracing identified a deterministic
Tk event cycle in `ProcessesPage`:

1. `<<TreeviewSelect>>` called the complete `update_view` method.
2. `update_view` called `fill`, which deleted every row, including the selected row.
3. `fill` inserted the rows again and restored the selection.
4. Deleting and restoring the selection generated another `<<TreeviewSelect>>` event.
5. The queued callbacks repeated the rebuild and starved ordinary Tk input and paint events.

No synchronous daemon or DMTCP call was found on the Tk thread. Refresh IPC and operation
polling remain on the background worker. The daemon and workload staying healthy during the
freeze is consistent with this GUI-only event storm. The source cycle is proven locally; the
reported Ubuntu symptom still requires a rerun on the target before it can be declared fixed
there.

### Changes made

- Bound `<<TreeviewSelect>>` to a selection-only renderer. That callback now changes only
  action eligibility and the details panel; it cannot repopulate its own `Treeview`.
- Added a Tk-independent selection policy that preserves a valid current row, selects a
  genuinely new row when appropriate, and supports explicitly selecting the workload ID
  returned by a successful launch.
- Made successful GUI launches register the returned workload ID before requesting the next
  snapshot, eliminating timing-dependent selection.
- Bounded each worker pump turn to 32 callbacks, bounded queued jobs to 64, kept the pump
  scheduled after callback exceptions, surfaced callback tracebacks, and stopped both GUI
  workers during orderly window close.
- Added a watchdog whose monitor thread observes a main-thread heartbeat without calling Tk.
  A stall longer than five seconds produces a mode-0600, size-capped Python thread-stack dump
  at the XDG NapGuard data location. It captures no frame locals.
- Expanded `gui --smoke` to connect, launch `c-single`, require automatic row selection,
  generate a selection event, prove a scheduled Tk heartbeat still runs, navigate all nine
  screens, stop the launched workload, and delete only that smoke-created history record.
  Existing user history is not reset or deleted.
- Made the final Ubuntu verifier require the new post-launch responsiveness success marker.

### Commands and actual results

| Command | Result |
|---|---|
| Focused worker/selection/watchdog/GUI-smoke selection | PASS: 7 passed, 1 skipped, 10 deselected; the skip requires Tkinter and an Ubuntu desktop display |
| `.venv/bin/ruff check .` | PASS |
| `.venv/bin/mypy` | PASS: no issues in 82 source files |
| `.venv/bin/python -m pytest -q` in the permitted environment, after all edits | PASS: **439 passed, 33 skipped**, 0 failed in 83.12 s |
| Same full suite in the restricted command sandbox | Interrupted after 132 passes because AF_UNIX `bind` and `ps` were denied; 15 displayed failures were sandbox `EPERM`/timeouts, not product regressions |
| `find scripts experiments -type f -name '*.sh' -exec bash -n {} +` | PASS |
| `.venv/bin/python -m pytest -q tests/docs/test_docs_consistency.py` | PASS: 73 passed |
| `bash scripts/final-ubuntu-check.sh 12e25d...` on macOS | Correctly refused with `run inside Ubuntu`, exit 2 |
| `git diff --check` | PASS before this report update; rerun before commit |

The 33 full-suite skips are explicitly platform-gated: Linux `/proc`, Linux/`ss`,
`SO_PEERCRED`, or Tk plus a desktop display. They are not counted as passes.

### Git diff summary before commit

- `src/napguard/ui/app.py`: non-recursive process selection, launch-result selection,
  callback error reporting, orderly worker/watchdog shutdown, and post-launch GUI smoke.
- `src/napguard/ui/selection.py`: pure refresh/selection policy.
- `src/napguard/ui/worker.py`: bounded queues/pump, callback isolation, and stop behavior.
- `src/napguard/ui/watchdog.py`: private, bounded event-loop stall diagnostics.
- `tests/unit/test_final_gui_worker.py`: bounded pump, error recovery, and stop regressions.
- `tests/unit/test_final_gui_responsiveness.py`: selection recursion/policy and watchdog tests.
- `tests/unit/test_final_packaging_ipc.py`: stronger GUI-smoke acceptance marker.
- `scripts/final-ubuntu-check.sh`: requires post-launch responsiveness evidence.
- Phase 10 plan/report: remediation scope, evidence, limitations, and gates.

No checkpoint image, daemon catalog, user workload, VM state, generated evidence directory,
secret, or host configuration was modified.

### Remediation gate table

| Criterion | Command/evidence | Status | Remaining risk |
|---|---|---|---|
| Selection callback cannot rebuild its own tree | AST regression plus selection-policy tests | PASS | Actual Tk event delivery remains target-gated |
| Newly launched workload selection is deterministic | Launch result ID is preferred before refresh; policy tests pass | PASS locally | Must be observed on Ubuntu/Tk |
| Worker cannot monopolize result delivery | 32-callback bound and backlog regression | PASS | Real desktop load remains target-gated |
| Callback failure does not stop result delivery | Exception-isolation regression | PASS | Dialog rendering remains target-gated |
| Stall produces bounded private evidence | Injected-clock watchdog test; mode 0600 and 256 KiB cap | PASS | A real hang dump is diagnostic evidence, not recovery |
| Full portable regression suite | 439 passed, 33 platform skips | PASS | Platform skips remain mandatory |
| Ruff / strict mypy / shell syntax / docs | Commands above | PASS | None known locally |
| Post-launch GUI smoke on a real display | Enhanced test exists; no Tk/display on this Mac | NOT RUN | Mandatory Ubuntu confirmation of the reported failure |
| Genuine DMTCP launch/checkpoint/restore | Final Ubuntu verifier steps 5–13 | NOT RUN | Mandatory; test doubles are not acceptance evidence |
| Ubuntu 24.04 ARM64 final verifier, clean SHA | `scripts/final-ubuntu-check.sh <NEW_SHA> --run-demo` | NOT RUN | Blocks presentation-ready and final-complete claims |

### Remediation status

**READY FOR UBUNTU VERIFICATION, not Ubuntu-verified.** The deterministic source defect is
removed and all locally executable gates pass. Phase 10 remains open because the exact
post-launch interaction, Linux-only suites, and genuine DMTCP workflow are mandatory and
`NOT RUN` on this macOS host. STOP after commit/push; do not advance to another phase.
