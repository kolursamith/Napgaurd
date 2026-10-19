# NapGuard

NapGuard is a planned Linux desktop application for opt-in checkpoint protection and recovery of compatible user workloads after power loss.

The project targets Ubuntu 24.04. Transparent checkpoint support will be compatibility-tested rather than assumed: DMTCP is the first candidate backend, CRIU is optional, and application-specific adapters may be required.

## Current status

Only Layer 0 repository and development-tooling foundations are present. There is no checkpoint, daemon, power, sleep, recovery, or GUI implementation yet.

## Host-side development setup

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/mypy src tests
```

Linux-specific development and verification must run on an explicitly verified Ubuntu target, not on macOS.

## Project layout

- `src/napguard/`: application packages, currently empty foundations only.
- `tests/`: unit, integration, and system test suites.
- `packaging/systemd/`: future Linux service packaging.
- `docs/architecture/`: component architecture.
- `docs/decisions/`: architectural decision records.
- `docs/test-reports/`: executed test evidence.
- `docs/phases/`: phase plans and completion reports.
- `experiments/`: isolated feasibility work for later approved phases.

