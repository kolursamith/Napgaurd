# NapGuard Codex Engineering Contract

You are building NapGuard, a Linux desktop application for selective, durable recovery of compatible user-selected workloads after power loss. macOS is the development host; Ubuntu 24.04 in UTM is the target. Do not implement Linux-specific features against macOS.

## Non-negotiable operating rules
1. Inspect the existing repository, Git status, tool versions, OS, architecture, permissions, and environment before changing anything. Preserve user changes; never overwrite existing files without inspection.
2. Work on exactly ONE phase per prompt. Never silently start the next phase, even if tests pass. Stop and report evidence; wait for explicit user approval.
3. Before edits, write `docs/phases/PHASE-N-plan.md` with scope, assumptions, tests, exit criteria, and rollback. After edits, write `docs/phases/PHASE-N-report.md` with actual commands, outputs, test results, known limitations, and Git diff summary.
4. Never invent test results, tool availability, Linux hardware signals, or checkpoint compatibility. Mark unexecuted tests NOT RUN and explain why.
5. Never call `kill -9` or process termination a battery exhaustion test. A full VM power-off and cold boot tests volatile-state loss, not real physical battery exhaustion.
6. No blanket support for arbitrary apps. Discover and test backend compatibility. DMTCP is the candidate primary backend; CRIU is optional and gated by feasibility. Support per-app adapters when transparent checkpointing fails.
7. Suspend freezes ordinary user processes. No battery polling during actual suspend. Use bounded pre-suspend checkpoints, and battery monitoring only while awake.
8. Preserve old valid checkpoints until new generations are durably committed. Do not claim durability until flush, fsync, rename, and directory sync (where applicable) have succeeded.
9. No unreviewed destructive actions, host power-off, battery draining, privilege escalation, broad package installation, firewall changes, or modifying global system config. Explain the change and request approval first. Use virtualenv and user services where practical.
10. Use typed Python, pytest, Ruff, mypy, structured logs, minimal dependencies, and reproducible lock/config files. Keep UI separate from daemon; avoid root where possible.
11. Use Git with small focused commits only when requested. Do not push, publish, or modify remote repos without explicit approval.
12. Each phase ends with a gate table: criterion, command/evidence, PASS/FAIL/NOT RUN, and remaining risk. Any required FAIL or NOT RUN blocks progression. Do not change a gate to make a failing implementation appear successful.
13. Do not fabricate or silently substitute mock battery/lid signals for physical integration. Label injected/replayed events explicitly.
14. Use safe command construction; no shell=True for user-supplied commands; validate paths and permissions; use explicit job ownership and least privilege.
15. On failures, diagnose and fix within the same phase; rerun all affected tests. If blocked, stop and ask for the minimal necessary user action.

## Layer execution rules
1. Work on exactly one approved layer at a time.
2. Never advance automatically to the next layer.
3. Run tests after every meaningful change.
4. Never report a test as passed unless its command actually executed successfully.
5. Preserve full failure output.
6. Never fabricate benchmark measurements.
7. Never replace real OS functionality with mocked behavior without clearly labeling the test.
8. Distinguish unit tests, integration tests, and real system tests.
9. Do not claim support for arbitrary applications.
10. Do not confuse process termination with battery exhaustion.
11. Do not claim battery monitoring works during genuine suspend.
12. Do not mark a layer complete when mandatory tests are blocked or skipped.
13. Do not change unrelated files.
14. Request approval before destructive operations or system configuration changes.
15. Record all important architectural decisions.

## Required phase sequence
0. Environment/repo reconnaissance and toolchain bootstrap.
1. Checkpoint engine feasibility with representative unmodified workloads; establish compatibility matrix.
2. Daemon skeleton, configuration, local authenticated IPC, persistent metadata.
3. Job registration, launch, supervision, opt-in protection, status reporting.
4. Durable checkpoint engine abstraction and integrity/retention guarantees.
5. Restore/recovery workflow and cold-boot test.
6. Multiple independent jobs and scheduling baselines.
7. Battery providers, replay provider, energy-aware scheduling experiments.
8. Lid/sleep integration with systemd-logind and bounded inhibitors.
9. PySide6 desktop application connected to daemon.
10. Security, packaging, end-to-end regression, docs, demonstration.

## Definition of done for any phase
- Implementation scoped to phase.
- Static checks and tests run and pass in the actual intended environment.
- Integration tests run when required; unsupported environment tests explicitly block the gate.
- No regressions in previous phases.
- Evidence saved to docs/phases and relevant tests.
- Explicit final STOP, not autonomous continuation.
