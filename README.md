# NapGuard

NapGuard is a planned Linux desktop application for opt-in checkpoint protection and recovery of compatible user workloads after power loss.

The project targets Ubuntu 24.04. Transparent checkpoint support will be compatibility-tested rather than assumed: DMTCP is the first candidate backend, CRIU is optional, and application-specific adapters may be required.

## Current status

- **Stage 02** (checkpoint feasibility): closed, PASS with recorded limitations.
- **Stage 03a** (DMTCP backend integration, developer mode): verified on Ubuntu 24.04 ARM64.
- **Stages 04–10 MVP** (catalog, supervisor, daemon/IPC, scheduler, battery, recovery, Tkinter GUI): implemented and Mac-tested, **awaiting Ubuntu verification**. See the [MVP report](docs/phases/MVP-report.md).

Developer mode only, on a single-user Ubuntu VM; not production-authorized ([ADR 0006](docs/decisions/0006-stage03-dmtcp-backend-integration-and-coordinator-security.md), [ADR 0008](docs/decisions/0008-mvp-developer-demo-integration.md)).

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

- `src/napguard/`: backend adapter (`checkpoint/`), process and network audits (`core/`), catalog (`storage/`), daemon, IPC, scheduler and manager (`daemon/`), battery (`power/`), GUI (`ui/`), self-test (`selftest/`).
- `tests/`: unit, integration, and system test suites.
- `packaging/systemd/`: future Linux service packaging.
- `docs/architecture/`: component architecture.
- `docs/decisions/`: architectural decision records.
- `docs/test-reports/`: executed test evidence.
- `docs/phases/`: phase plans and completion reports.
- `experiments/`: isolated feasibility work for later approved phases.

