# Stage 00 Report: Repository Audit, Workflow Setup, and Linux Baseline

- Date: 2026-10-19
- Branch: `stage-00/workflow-baseline` (no commits yet)
- Verdict: **BLOCKED** — no Ubuntu baseline results have been reviewed yet.
- **Addendum (2026-10-19, during Stage 01):** The user reported that all mandatory Ubuntu checks passed on commit `931369dab8c84534160821039b863f38c7f737b3`. The verdict is therefore **PASS, based on user-reported results**; see [Ubuntu results](#addendum-ubuntu-results-user-reported) below. The original text that follows is preserved as written before the Ubuntu run.

Stage 00 continues the earlier Phase/Layer 0 work under the approved workflow (macOS → Git → GitHub → Ubuntu in UTM, no SSH). The historical records are preserved unchanged apart from a forward pointer:

- [`PHASE-0-plan.md`](PHASE-0-plan.md) and [`PHASE-0-report.md`](PHASE-0-report.md)
- [`../test-reports/layer-0.md`](../test-reports/layer-0.md)

The Phase 0 SSH blocker (host-key verification failure) no longer applies, because the approved workflow does not use SSH. Linux facts recorded there as BLOCKED remain unverified until the Ubuntu run below.

Scope: audit, documentation, `.gitignore`, and the Ubuntu verification script. No checkpoint engine, GUI, daemon, scheduler, or recovery code was written, and DMTCP/CRIU were not installed.

## 1. Existing repository audit

Audited 2026-10-19 on macOS.

| Item | Finding |
|---|---|
| Session folder | `/Users/sogo/genai/TEMP/Projects/OS_project/OS_PRO`: empty and not a Git repository. Left untouched. |
| Actual repository | `/Users/sogo/genai/TEMP/Projects/OS_project/project` (`git rev-parse --show-toplevel`) |
| Branch | `main` with no commits. Stage 00 work moved to the new branch `stage-00/workflow-baseline`, also with no commits. |
| Commits | None (`git log`: "does not have any commits yet") |
| Remotes | None (`git remote -v` printed nothing) |
| Working tree | All 27 project files are untracked. Nothing has ever been committed. |
| Sensitive files | None tracked (nothing is tracked). A pattern scan of the untracked files for keys, tokens, and passwords found only prose in the docs. There are no `.env`, key, socket, or database files outside ignored caches. |
| Mac `.venv` | Exists, built with Homebrew Python 3.14.7. It is ignored and Mac-only. |

Repository contents:

- Source: `src/napguard/__init__.py` (`__version__ = "0.1.0"`), `py.typed`, and eight empty subpackages with docstrings only: `checkpoint`, `core`, `daemon`, `power`, `recovery`, `sleep`, `storage`, `ui`.
- Tests: `tests/unit/test_import.py` (one version-import smoke test). `tests/integration/` and `tests/system/` contain only `.gitkeep`.
- Python configuration: `pyproject.toml` (hatchling, `requires-python >=3.12`, no runtime dependencies, dev extras mypy/pytest/Ruff, strict mypy, Ruff rules `B,E4,E7,E9,F,I,UP`).
- Docs: `docs/architecture/overview.md`, `docs/decisions/0001-technology-stack.md`, the Phase 0 plan and report, and the Layer 0 test report.
- Scripts: `scripts/layer0-ubuntu-check.sh`. It was written for SSH use, created `.venv` inside the checkout, wrote a marker under `~/.local/share`, and only printed output, with no PASS/FAIL/SKIP verdict or failing exit status.
- Agent rules: `AGENTS.md`, plus `README_AGENT_RULES.md`, an older copy of the same contract without the "Layer execution rules" section.

What is implemented, planned, run, and unverified:

- **Implemented:** package skeleton, one import smoke test, and tooling configuration only.
- **Planned only:** every NapGuard capability (checkpointing, daemon, IPC, storage, power and sleep handling, recovery, GUI).
- **Actually run:** Mac-side import, pytest, Ruff, and mypy, on 2026-10-11 (Phase 0 report) and again on 2026-10-19 (below).
- **Unverified:** every Linux fact (Ubuntu release, runtime architecture, Python 3.12 compatibility, systemd, D-Bus, storage). No Linux command has ever run against this repository.

## 2. Existing work reused

- The package, test, `pyproject.toml`, and Mac `.venv` were reused as they are. Nothing was regenerated or duplicated.
- `docs/architecture/overview.md` was extended in place.
- `scripts/layer0-ubuntu-check.sh` was revised in place rather than duplicated. It keeps its name so the historical references stay valid.
- The ADR, Phase 0 plan, Phase 0 report, and Layer 0 test report were preserved. Only a short forward pointer was added to the two reports.

## 3. Architecture consistency review

The repo's architecture overview was compared with the Stage 00 target design and with the master blueprint `NAPGUARD_SYSTEM_ARCHITECTURE.docx`. That blueprint lives outside the repository in the parent folder.

- **Already consistent:** PySide6 GUI as an unprivileged client, user-level daemon, Unix-domain socket IPC, SQLite metadata kept separate from filesystem checkpoint images, generation durability rules, power monitoring only while awake, bounded logind inhibitors, and DMTCP first with CRIU optional.
- **Added to `overview.md`:**
  - The target system diagram.
  - ARM64 Ubuntu as the first-release target, as stated in the master blueprint.
  - The 12 architectural invariants.
  - The development and verification workflow.
  - A note that the workload registry, process supervisor, compatibility manager, scheduler, and event logger have no packages yet. They will be created in their own stages, not as empty placeholders.
- **No new source modules were created.** The eight existing empty subpackages from Layer 0 were kept.

## 4. Mac baseline results

macOS 14.8.1 (23J30), arm64, Python 3.14.7 (Homebrew), Git 2.39.5. These are **development-host results only**, not Linux evidence. They were run from the repository root using the existing `.venv`.

| Command | Exit | Result |
|---|---|---|
| `.venv/bin/python -c "import sys, napguard; ..."` | 0 | `0.1.0`, imported from `src/napguard/__init__.py` (editable install) |
| `.venv/bin/python -m pytest` | 0 | `1 passed` (pytest 9.1.1) |
| `.venv/bin/ruff check .` | 0 | `All checks passed!` (Ruff 0.17.0) |
| `.venv/bin/mypy src tests` | 0 | `Success: no issues found in 10 source files` (mypy 1.20.2) |

Dry run of the revised script's install and test commands on macOS, under bash, in a throwaway venv in a temporary directory:

| Step | Exit | Result |
|---|---|---|
| `python3 -m venv $T/venv` | 0 | created |
| `$T/venv/bin/python -m pip install "$REPO[dev]"` | 0 | non-editable wheel install from `pyproject.toml` |
| import check | 0 | imported from `$T/venv/lib/python3.14/site-packages/napguard/` |
| `pytest -p no:cacheprovider` | 0 | `1 passed` |
| `ruff check --no-cache .` | 0 | `All checks passed!` |
| `mypy --cache-dir $T/mypy-cache src tests` | 0 | `Success: no issues found in 10 source files` |
| `git status --porcelain --ignored` before and after | — | identical: the checkout was not modified |

Other script checks:

- `bash -n scripts/layer0-ubuntu-check.sh`: syntax OK.
- Running the script on macOS correctly refused with `FAIL mandatory Linux kernel — uname -s reports Darwin` and exited 1.

The Linux-specific sections of the script have **not** been executed anywhere yet.

## 5. Ubuntu baseline test instructions

`scripts/layer0-ubuntu-check.sh` now:

- runs as an ordinary user, with no sudo, no package installation, and no SSH;
- creates its venv, pip cache, and mypy cache in a `mktemp` directory under `/tmp` that is deleted on exit;
- probes persistent storage with a temporary directory under `$HOME`, deleted right after the probe, and fails if `$HOME` is on tmpfs or ramfs;
- turns off pytest, Ruff, and bytecode caches in the checkout, and fails if the checkout changed during the run;
- reports each check as PASS, FAIL, or SKIP, marked MANDATORY or OPTIONAL, with a reason, then prints a summary and verdict;
- exits 0 for PASS, 1 for any mandatory FAIL, and 2 for any mandatory SKIP (BLOCKED).

**Mandatory checks:**

- Linux kernel and a non-root user
- Ubuntu release recorded
- Architecture recorded
- Git available, Git checkout, HEAD recorded
- HEAD matches the expected Mac SHA
- Clean working tree
- Python ≥ 3.12
- systemd is PID 1
- System D-Bus answers `GetId`
- Writable persistent storage
- venv created
- Package installs from `pyproject.toml`
- Installed package imports
- pytest, Ruff, mypy
- Expected repository paths present
- Checkout unchanged by the script

**Optional checks** (recorded for later stages; they do not block Stage 00):

- VERSION_ID 24.04
- aarch64
- `systemctl is-system-running` = running
- systemd user manager reachable
- logind `CanSuspend`
- `dmtcp_launch` and `criu` presence (inspection only)

Exact commands are in section 6 and in the final chat response for this stage.

## 6. GitHub synchronization status

- **No remote exists.** No GitHub URL has been invented, and none was created, added, or pushed.
- Nothing is committed. The proposed initial commit contains the 27 audited files plus this stage's changes (section 8). Excluded by `.gitignore`: `.venv/`, caches, `__pycache__/`, `.DS_Store`.
- Recommended setup, done by the user or with explicit approval:
  1. Create ONE **private** GitHub repository, for example `napguard`, with no README, license, or `.gitignore`.
  2. On the Mac:
     ```sh
     git remote add origin https://github.com/<user>/napguard.git
     git push -u origin stage-00/workflow-baseline
     git push origin stage-00/workflow-baseline:main
     ```
     The second push creates `main` at the same commit so the default branch has history.
  3. In Ubuntu, clone with a read-only credential, such as a fine-grained token with Contents: read on that one repository. Enter it when Git prompts, and never store it in the repository.
- **Commit identity:**
  - Run `git rev-parse HEAD` on the Mac after committing.
  - Pass that SHA to the script in Ubuntu. The script fails if Ubuntu's HEAD differs or if the Ubuntu working tree is dirty.

## 7. Open issues and blockers

1. **BLOCKER:** no Ubuntu results exist. Stage 00 cannot pass until the script's output from Ubuntu is supplied and reviewed.
2. **BLOCKER:** no commit and no GitHub remote yet, so Ubuntu cannot pull the source. Both need user approval.
3. Ubuntu 24.04 ships Python 3.12; the Mac uses 3.14. The Ubuntu run will be the first 3.12 run of the toolchain.
4. Ubuntu may lack `git` or `python3-venv`. The script reports this as a FAIL with a hint; installing them (`sudo apt install git python3-venv`) is the user's action.
5. The pip install needs PyPI access from the VM.
6. `README_AGENT_RULES.md` is an outdated duplicate of `AGENTS.md`. It was preserved because it is user-owned; the user should decide whether to keep it.
7. The master architecture and roadmap `.docx` files are outside the repository and not version-controlled.
8. `AGENTS.md` rule 3 asks for a separate plan file. Per the Stage 00 instruction ("a single Stage 00 workflow report"), the scope is recorded at the top of this report instead.
9. The `checkpoints/` ignore pattern matches any directory with that name. The source package is `checkpoint` (singular), which is unaffected.

## 8. Exact changed files

| File | Change |
|---|---|
| `.gitignore` | Added venv, coverage, credential/env, SQLite, socket, checkpoint image and memory dump, and experiment artifact patterns. Verified that all 27 project files remain unignored. |
| `docs/architecture/overview.md` | Added the ARM64 target, the target diagram, invariants, and the workflow. Existing sections are unchanged. |
| `scripts/layer0-ubuntu-check.sh` | Rewritten in place: removed SSH, network address listing, the in-checkout `.venv`, and the `~/.local/share` marker. Added PASS/FAIL/SKIP results, commit identity, temporary venv, and a read-only self-check. Made executable. |
| `docs/phases/STAGE-00-report.md` | New: this report. |
| `docs/phases/PHASE-0-report.md` | Added a forward pointer to this report at the top. |
| `docs/test-reports/layer-0.md` | Added a forward pointer to this report at the top. |

Git state changed: created the branch `stage-00/workflow-baseline` while `main` had no commits. No commit, remote, or global configuration was changed.

## 9. Stage verdict

| Gate criterion | Evidence | Result |
|---|---|---|
| Repository available inside Ubuntu | — | NOT RUN |
| Ubuntu commit matches Mac revision | — | NOT RUN (no commit exists yet) |
| Ubuntu release and architecture recorded | — | NOT RUN |
| Python venv can be created | — | NOT RUN |
| Project installs from declared packaging | Mac dry run only | NOT RUN on Ubuntu |
| Package imports | Mac only | NOT RUN on Ubuntu |
| pytest passes | Mac only | NOT RUN on Ubuntu |
| Ruff passes | Mac only | NOT RUN on Ubuntu |
| mypy passes | Mac only | NOT RUN on Ubuntu |
| Linux system prerequisites checked | — | NOT RUN |
| No unaddressed mandatory failure | Items 1–2 in section 7 | BLOCKED |

**Stage 00: BLOCKED.** STOP: do not start Stage 01.

## Addendum: Ubuntu results (user-reported)

Recorded 2026-10-19 during Stage 01. The user ran `scripts/layer0-ubuntu-check.sh` inside the UTM VM and reported that every mandatory check passed on commit `931369dab8c84534160821039b863f38c7f737b3`, with this environment:

- Ubuntu 24.04.5 LTS, aarch64 (ARM64), Python 3.12.3
- systemd and the system D-Bus available
- Git, virtual environment creation, package install and import, pytest, Ruff, and mypy all passing
- checkout clean
- DMTCP and CRIU not installed (not evaluated, as intended for Stage 00)

The raw script log was not supplied to Claude, so the per-check output is not reproduced here. If the log is added later, it should be attached under `docs/test-reports/`.

GitHub synchronization, superseding section 6:

- `origin` is `https://github.com/kolursamith/Napgaurd.git` (public, by the user's choice).
- `stage-00/workflow-baseline` was pushed normally (no force), and `git ls-remote` confirmed the remote branch at `931369dab8c84534160821039b863f38c7f737b3`.
- No `main` branch exists yet; GitHub's default branch is currently `stage-00/workflow-baseline`.

**Updated verdict: Stage 00 PASS (user-reported Ubuntu evidence).**
