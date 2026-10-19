# Phase 0 Plan: Environment, Feasibility, and Repository Foundation

## Scope

- Discover the actual execution host and available development tools.
- Determine whether the intended Ubuntu 24.04 target is genuinely accessible.
- Create only the portable NapGuard repository foundation and minimal package smoke test.
- Configure a local virtual environment with pytest, Ruff, and mypy when possible.
- Record the proposed architecture and initial technology decision.
- Execute and record Layer 0 tests 0.1 through 0.10.
- Stop after Phase/Layer 0; do not implement checkpointing, daemon, power, sleep, recovery, or UI behavior.

## Assumptions

- This workspace is the intended repository root.
- The existing `AGENTS.md` and `README_AGENT_RULES.md` are user-owned and must be preserved.
- macOS is suitable only for portable repository/tooling work; Linux-specific behavior requires a verified Ubuntu target.
- No Ubuntu connection address or authenticated access method has yet been supplied or verified.
- Python 3.14.7 is the currently detected host interpreter; project metadata will retain compatibility with Python 3.12+ unless tooling proves otherwise.

## Planned verification

1. Record OS, kernel, architecture, execution context, filesystem, user, permissions, and tool versions.
2. Verify workspace writability with a temporary file that is removed immediately.
3. Inspect UTM availability and any non-secret evidence of a reachable Ubuntu target.
4. Initialize Git if this directory is not already a repository.
5. Create the minimal package tree, configuration, documentation, and a real smoke test.
6. Create `.venv` and install only the Layer 0 development tools: pytest, Ruff, and mypy.
7. Run package import, pytest, Ruff, and mypy checks.
8. Verify required paths and inspect final Git status for unintended changes.
9. Record Linux facilities as verified, failed, or blocked based only on commands actually executed on Ubuntu.

## Exit criteria

- Portable repository structure and configuration exist.
- Package import, pytest smoke test, Ruff, and mypy execute successfully on the current host.
- Architecture, decision, phase report, and Layer 0 test report exist.
- Every required Layer 0 test has a recorded PASS, FAIL, BLOCKED, or NOT RUN result.
- Phase 0 is PASSED only if the intended Ubuntu target is verified and all mandatory checks pass; otherwise it is BLOCKED or FAILED.

## Rollback

- Remove only files introduced by Phase 0 after reviewing `git status`/the recorded file inventory.
- Remove the local `.venv` to undo installed Python tooling.
- Do not alter host power settings, UTM configuration, global packages, system services, or external repositories.
