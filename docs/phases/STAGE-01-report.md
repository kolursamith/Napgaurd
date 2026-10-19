# Stage 01 Report: Requirements and Detailed System Design

- Date: 2026-10-19
- Baseline: `931369dab8c84534160821039b863f38c7f737b3` on `stage-00/workflow-baseline` (Stage 00 PASS, user-reported Ubuntu evidence)
- Branch: `stage-01/requirements-design`, created from the Stage 00 baseline. The commit SHA is recorded in Git history and in the Ubuntu logs.
- Verdict: **READY FOR LINUX REVIEW.** This is not PASS: the Ubuntu gate has not run.

## Scope

Stage 01 produces the requirements, the component contracts, the data and state models, the backend contract, the storage, recovery, power, sleep, and IPC/security designs, the traceability matrix, and the Linux-assumption probe needed for the Ubuntu gate.

Stage 01 does not implement checkpointing, storage, the daemon, the scheduler, or the GUI. It does not install DMTCP or CRIU. It makes no claim that either backend works on ARM64.

## Inputs reviewed

- `AGENTS.md`, `README_AGENT_RULES.md`, `README.md`, `pyproject.toml`
- `docs/architecture/overview.md`, `docs/decisions/0001-technology-stack.md`
- `docs/phases/PHASE-0-plan.md`, `PHASE-0-report.md`, `STAGE-00-report.md`, `docs/test-reports/layer-0.md`
- The master blueprint `NAPGUARD_SYSTEM_ARCHITECTURE.docx` and `ROADMAP_NapGuard.docx`, both in the parent folder outside the repository. They are the source of the approved Stage 00–12 roadmap and of the data model and security principles.
- The existing source (`src/napguard/**`), tests, and scripts.

## Deliverables

| Task | Document |
|---|---|
| 1. Requirements (115 IDs across 11 categories, each with acceptance criterion and verification method) | [requirements.md](../requirements/requirements.md) |
| 2. Component architecture, contracts, ownership, sync/async | [components.md](../architecture/components.md) |
| 3. Data models and four state machines with invalid transitions and crash recovery | [data-model.md](../architecture/data-model.md) |
| 4. Backend-neutral contract; DMTCP vs CRIU (unverified) | [checkpoint-backend.md](../architecture/checkpoint-backend.md) |
| 5. Storage, publication protocol, crash table, retention, quota, recovery selection | [storage-recovery.md](../architecture/storage-recovery.md), [ADR 0003](../decisions/0003-checkpoint-publication-and-catalog-durability.md) |
| 6. Power and sleep design, inhibitor lifetime, simulated-input policy | [power-sleep.md](../architecture/power-sleep.md) |
| 7. IPC protocol, error codes, authentication, security controls | [ipc-security.md](../architecture/ipc-security.md), [ADR 0002](../decisions/0002-ipc-protocol-and-authentication.md) |
| 8. Traceability matrix against the Stage 00–12 roadmap | [traceability.md](../requirements/traceability.md) |
| 9. Documentation review, assumptions, decisions, risks | this report and [overview.md](../architecture/overview.md) |
| 10. Validation | `tests/docs/test_docs_consistency.py`, plus pytest, Ruff, and mypy (below) |
| Ubuntu gate tool | `scripts/stage01_linux_assumptions.py` |

## Assumptions to verify on Ubuntu

| ID | Assumption | Verified by |
|---|---|---|
| A-01 | The UTM VM exposes no battery under `/sys/class/power_supply` | assumptions script, optional check |
| A-02 | logind is reachable on the system bus, exposes `PrepareForSleep`, and reports `InhibitDelayMaxUSec` (expected 5 s) | assumptions script |
| A-03 | An ordinary user can take a sleep *delay* inhibitor | assumptions script |
| A-04 | `pidfd_open` and `pidfd_send_signal` work, and `/proc/<pid>/stat` field 22 is readable | assumptions script, mandatory |
| A-05 | `SO_PEERCRED` works on `AF_UNIX` sockets | assumptions script, mandatory |
| A-06 | `$XDG_RUNTIME_DIR` exists with mode 0700 in the user's session | assumptions script, mandatory |
| A-07 | `$XDG_DATA_HOME` is on a persistent filesystem (expected ext4) that supports directory fsync and atomic rename | assumptions script, mandatory |
| A-08 | System SQLite supports WAL, `synchronous=FULL`, and partial unique indexes | assumptions script, mandatory |
| A-09 | A transient systemd user scope can be created (OD-03) | assumptions script, optional |
| A-10 | `dmtcp` and/or `criu` have install candidates in the Ubuntu 24.04 arm64 archive | assumptions script, optional (read-only `apt-cache policy`) |
| A-11 | The VM's suspend support is unknown. `/sys/power/state` is recorded, but no suspend is performed. | assumptions script, optional |

## Open decisions

| ID | Decision | Decide in | Notes |
|---|---|---|---|
| OD-01 | Primary backend: DMTCP or CRIU | Stage 02 (ADR) | Depends entirely on feasibility on the actual VM |
| OD-02 | CRIU privilege model (file capabilities or root) if CRIU is chosen | Stage 02 (ADR + explicit user approval) | Conflicts with SEC-010 unless approved |
| OD-03 | Process containment: a transient scope per workload, or `KillMode=process` | Stage 05/06 | Required for FR-030. Scopes preferred. A-09 tests feasibility. |
| OD-04 | D-Bus client library for logind | Stage 08 (ADR, NFR-002) | Pure-Python preferred. Qt's D-Bus would pull Qt into the daemon. |
| OD-05 | Packaging format (venv + installer script, pipx, or `.deb`) | Stage 11 | PKG-001 needs no root for NapGuard itself |
| OD-06 | Encryption at rest for checkpoint images | Post-v1 | Images contain process memory (SEC-008 warning only in v1) |
| OD-07 | `AGENTS.md` lists Phases 0–10. The approved roadmap is Stages 00–12. | User | Mapping in [traceability.md](../requirements/traceability.md#approved-stage-roadmap). `AGENTS.md` is user-owned and was not edited. |
| OD-08 | Report folder: the blueprint suggests `docs/stages/`, but the repository uses `docs/phases/` | User | Kept `docs/phases/` to avoid a duplicate hierarchy |
| OD-09 | Initial numeric defaults (quota 10 GiB, reserve, NFR-007 targets, verify window 10 s) | Revisit after Stage 02 measurements | Changes recorded by ADR |
| OD-10 | IPC schema validation: hand-written validators or a library | Stage 06 | No new dependency without an ADR |
| OD-11 | Development location: the blueprint says "directly inside Ubuntu", but the approved workflow is Mac → GitHub → Ubuntu | User | This stage follows the approved workflow |
| OD-12 | A second local Ubuntu test user is needed to test SEC-002 | Stage 06 (needs approval; account creation is the user's action) | Without it, SEC-002's cross-user test is BLOCKED |

## Compatibility and project risks

| ID | Risk | Likelihood / impact | Mitigation |
|---|---|---|---|
| R-01 | Neither DMTCP nor CRIU works on Ubuntu 24.04 aarch64 in UTM | medium / critical | Stage 02 runs first, with a hard stop. Reassess the target (for example x86-64 VM or a different backend). |
| R-02 | DMTCP is not packaged for arm64, so a source build is needed and ARM64 support may be immature | high / high | Record the build steps. Treat ARM64 DMTCP as unverified until the trials pass. |
| R-03 | CRIU needs privileges, which conflicts with SEC-010 | high / high | OD-02. The non-root mode needs capabilities on the binary, which requires user approval. |
| R-04 | Checkpoint duration exceeds the roughly 4 s pre-suspend budget | high / medium | Periodic checkpoints are the primary protection. Pre-suspend is best effort and measured (PRF-006). |
| R-05 | QEMU/UTM disk cache mode weakens durability on host failure | medium / medium | REL-007 tests guest power-off only, and is labeled as such. Record the UTM disk settings in Stage 09/11. |
| R-06 | Restored processes land in the daemon's cgroup or are killed with it | medium / high | OD-03. Stage 02 records the process trees. |
| R-07 | Large images exhaust the VM disk | medium / medium | REL-010 quotas, measured sizes (PRF-002) |
| R-08 | PySide6 wheels and system Qt libraries on Ubuntu 24.04 aarch64 | medium / medium | Check during Stage 10 planning. The GUI is isolated behind IPC (NFR-005). |
| R-09 | GUI and network workloads are unsupported by the backend | high / low | Expected. They are reported via CMP-001, LIM-003, and LIM-004. |
| R-10 | The VM cannot suspend, so a real `PrepareForSleep` cannot be observed | medium / medium | Use a labeled simulated logind for integration tests, and attempt a real VM suspend only with approval (power design). |

## Files changed

| File | Change |
|---|---|
| `docs/requirements/requirements.md` | **new**: requirements specification |
| `docs/requirements/traceability.md` | **new**: roadmap and traceability matrix |
| `docs/architecture/components.md` | **new**: component contracts |
| `docs/architecture/data-model.md` | **new**: entities and state machines |
| `docs/architecture/checkpoint-backend.md` | **new**: backend contract |
| `docs/architecture/storage-recovery.md` | **new**: storage and recovery design |
| `docs/architecture/power-sleep.md` | **new**: power and sleep design |
| `docs/architecture/ipc-security.md` | **new**: IPC and security design |
| `docs/decisions/0002-ipc-protocol-and-authentication.md` | **new**: ADR |
| `docs/decisions/0003-checkpoint-publication-and-catalog-durability.md` | **new**: ADR |
| `docs/phases/STAGE-01-report.md` | **new**: this report |
| `docs/architecture/overview.md` | added a "Detailed design (Stage 01)" link section; existing text unchanged |
| `docs/phases/STAGE-00-report.md` | added a user-reported Ubuntu results addendum and the GitHub status; the original text is preserved |
| `tests/docs/test_docs_consistency.py` | **new**: documentation consistency tests |
| `scripts/stage01_linux_assumptions.py` | **new**: read-only Linux assumption probe for the Stage 01 Ubuntu gate |

Unchanged: all source code, `pyproject.toml`, `AGENTS.md`, ADR 0001, the Phase 0 and Layer 0 reports, and `scripts/layer0-ubuntu-check.sh`.

## Mac results

macOS 14.8.1 arm64, using the existing `.venv` (Python 3.14.7). These are **not Linux evidence.**

| Command | Exit | Result |
|---|---|---|
| `.venv/bin/python -m pytest` | 0 | `42 passed`: 1 import smoke test plus 41 documentation consistency tests |
| `.venv/bin/ruff check .` | 0 | `All checks passed!` |
| `.venv/bin/mypy src tests` | 0 | `Success: no issues found in 11 source files` |
| `.venv/bin/mypy --strict --platform linux --python-version 3.12 scripts/stage01_linux_assumptions.py` | 0 | `Success: no issues found in 1 source file` (checked against Linux stubs because the script is Linux-only) |
| `python3 -I scripts/stage01_linux_assumptions.py` | 1 | correctly refuses on macOS: `FAIL mandatory Linux host — Darwin` |
| Negative control: a copy of the docs with an unknown functional-requirement ID (number 999) and a broken anchor added | — | both detected (2 failed, 39 passed), so the consistency tests are not vacuous |

Notes on the earlier failures in this stage:

- The first test run failed only because the two links to this report came before the report existed.
- The first Ruff run flagged a redundant Python-version guard (UP036), which was removed.
- The first mypy run of the script used macOS stubs, which lack `pidfd_open`, `pidfd_send_signal`, and `SO_PEERCRED`. It was rerun with `--platform linux`.

The Linux-only checks in `scripts/stage01_linux_assumptions.py` have **not run anywhere yet**.

## Ubuntu verification instructions

After the user approves committing and pushing the Stage 01 changes, run these in the Ubuntu VM:

```sh
cd ~/napguard
git fetch origin
git checkout <stage-01 branch>
git pull --ff-only
git rev-parse HEAD                     # must equal the Mac commit SHA
bash scripts/layer0-ubuntu-check.sh <MAC_COMMIT_SHA> 2>&1 | tee ~/stage01-baseline.log
python3 -I scripts/stage01_linux_assumptions.py 2>&1 | tee ~/stage01-assumptions.log
```

The baseline script re-verifies commit identity, a clean tree, install, import, pytest (including the documentation consistency tests), Ruff, and mypy on Python 3.12. The assumptions script verifies A-01 to A-11.

## Gate table

| Criterion | Evidence | Result |
|---|---|---|
| Every core requirement has an ID, an acceptance criterion, and a verification method | `test_every_requirement_has_criterion_and_verification` | PASS (Mac) |
| Every requirement maps to a component and a stage | `test_traceability_*` | PASS (Mac) |
| All requirement IDs and cross-references resolve | `test_referenced_requirement_ids_exist`, `test_relative_links_and_anchors_resolve` | PASS (Mac) |
| No implementation of later-stage features | file list above; review | PASS |
| Backend behaviour marked unverified | [checkpoint-backend.md](../architecture/checkpoint-backend.md) | PASS (review) |
| pytest, Ruff, and mypy on Ubuntu at the Stage 01 commit | baseline script log | NOT RUN |
| Linux assumptions A-01 to A-11 verified on Ubuntu | assumptions script log | NOT RUN |

**STOP: Stage 02 has not been started.**
