# Phase 10 Plan — Final Stabilization and Release Gate

Date: 2026-10-24

## Scope

- Preserve the current architecture and developer-only DMTCP security boundaries while stabilizing the existing Phase 10/MVP implementation.
- Diagnose the nine Ubuntu errors reported at `tests/unit/test_final_history.py:23` and every locally reproducible daemon/IPC failure from the interrupted macOS run.
- Make the smallest evidence-backed production or test fixes, with focused regression coverage.
- Audit and repair the final Ubuntu verifier only where an actual defect is demonstrated.
- Validate the existing supported application surfaces: catalog/history, daemon/IPC, process supervision, scheduler, simulated battery triggers, recovery verification, GUI presenter/worker behavior, packaging, and documentation consistency.
- Run all safe local static and automated checks. Keep Linux/DMTCP/real-display checks explicitly pending unless executed on Ubuntu.
- If the local gates pass, commit the Phase 10 changes and push the existing `stage-03/backend-integration` branch to `origin` as the user requested.

## Assumptions and constraints

- The fetched remote branch and local branch both start at `b5ae58fa219a37841c10316869861dfee0ca20b9`.
- The complete Ubuntu evidence directory `/home/samith/napguard-final-evidence-20261022T093656Z` is not present on macOS. Only the reported failure location and counts are available.
- macOS and test doubles can validate portable orchestration logic, but cannot verify Ubuntu, `/proc`, `SO_PEERCRED`, Tk/X11, DMTCP 4.2.0, real checkpoint/restore, systemd, or desktop launcher behavior.
- No unsupported workload class, production authorization, cold-boot automation, external filesystem rollback, or physical battery/suspend claim will be added.
- No root action, global configuration change, destructive cleanup, or force push is authorized.

## Planned tests and evidence

1. Capture complete tracebacks for the focused history test under the available pytest version and for every daemon/IPC failure.
2. Compare the Ubuntu failure shape against pytest-version behavior without inventing unavailable traceback content.
3. After each fix, rerun the focused test, its module, and affected subsystem modules.
4. Run the full pytest suite and record exact pass/fail/skip totals.
5. Run Ruff, strict mypy, shell syntax checks, documentation checks, packaging tests, and `git diff --check`.
6. Inspect `scripts/final-ubuntu-check.sh` for revision, clean-tree, isolation, mandatory-test, GUI, DMTCP, cleanup, leak, and exit-status behavior.
7. Record all Ubuntu-only gates as `NOT RUN` with exact rerun commands.

## Exit criteria

- The root cause of the nine-error Ubuntu cluster is identified from available evidence and fixed with regression coverage.
- Every locally reproducible product or test defect in the requested Phase 10 scope is fixed and affected tests pass.
- Full local pytest, Ruff, strict mypy, shell syntax, documentation, packaging, and diff checks pass, except platform-mandated skips that are truthfully recorded.
- `docs/phases/PHASE-10-report.md` records commands, results, limitations, diff summary, and a PASS/FAIL/NOT RUN gate table.
- A mandatory failure or blocked local check prevents completion and push.
- Ubuntu-only acceptance remains a blocking `NOT RUN` for an Ubuntu-verified claim; the strongest permissible verdict without it is `READY FOR UBUNTU VERIFICATION`.
- No work begins on a later phase.

## Rollback

- Revert only the focused Phase 10 commit(s) created during this phase using normal Git history; do not rewrite shared history.
- Test fixtures use temporary directories and test-owned sockets/processes. They must not touch real NapGuard data, services, checkpoints, or user configuration.
- Existing checkpoint generations, user data, systemd units, and global host configuration remain unchanged.
