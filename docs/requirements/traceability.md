# NapGuard Stage Traceability Matrix

- Status: Stage 01 draft for Linux review
- Related: [requirements](requirements.md), [components](../architecture/components.md)

This matrix maps every requirement in [requirements.md](requirements.md) to its responsible component (a key from the [component index](../architecture/components.md#component-index)), the stage that delivers it, and the evidence that closes it. An automated check (`tests/docs/test_docs_consistency.py`) enforces that the two documents use the same ID set. "—" means the test level does not apply. A requirement's stage gate cannot pass while any non-"—" cell for it lacks evidence.

## Approved stage roadmap

| Stage | Scope | Mandatory Ubuntu gate |
|---|---|---|
| 00 | Workflow setup | Clone or pull the repo and run the baseline checks on Ubuntu |
| 01 | Requirements and design | Verify the Linux assumptions and available system interfaces |
| 02 | **Checkpoint feasibility (highest priority)** | Real checkpoint → terminate → restore, with proven state continuity |
| 03 | Backend integration: API, adapter, capability detection, CLI | Real operations work through NapGuard's API |
| 04 | Storage and catalog | A persisted checkpoint survives restart, and corrupt or incomplete ones are rejected |
| 05 | Workload management: registry, supervisor | Two independently managed workloads behave correctly |
| 06 | Background daemon and IPC | A real systemd user service handles authenticated local requests |
| 07 | Scheduling | Real scheduled checkpoints, with no unsafe overlapping operations |
| 08 | Power and sleep | Real available Linux interfaces, plus clearly labeled simulated battery tests |
| 09 | Recovery | Cold VM reboot → restore from disk → proven resumed state |
| 10 | Desktop GUI | The GUI performs real operations end to end |
| 11 | Packaging | Clean installation and removal on supported Ubuntu |
| 12 | Final validation | Full regression suite and a reproducible live demo |

The phase list in `AGENTS.md` (Phases 0–10) predates this roadmap. Approximate mapping:

| AGENTS.md phase | Approved stage |
|---|---|
| 0 | 00 + 01 |
| 1 | 02 |
| 2 (daemon, IPC, metadata) | split into 04 and 06 |
| 3 | 05 |
| 4 | 03 + 04 |
| 5 | 09 |
| 6 | 07 |
| 7 + 8 | 08 |
| 9 | 10 |
| 10 | 11 + 12 |

Reconciling `AGENTS.md` itself is open decision OD-07.

## Matrix

| ID | Component | Stage | Unit | Integration | Linux system | Failure | Acceptance evidence |
|---|---|---|---|---|---|---|---|
| FR-001 | registry | 05 | UUID assignment, spec validation | register → reload from catalog | register real binary, restart daemon (06), reboot (09) | duplicate name rejected | Stage 05 test log; Stage 09 reboot record |
| FR-002 | registry | 05 | each invalid-input case | IPC returns `INVALID_ARGUMENT` (06) | real file modes and ownership | symlink to world-writable target, NUL in an argument | Stage 05 test log |
| FR-003 | registry | 05 | update, disable, remove rules | remove while running → `CONFLICT` | — | purge vs keep checkpoints | Stage 05 test log |
| FR-004 | registry | 05 | list projection | `workload.list` over IPC (06) | — | — | Stage 05/06 test logs |
| FR-005 | compat | 03 | status and reason mapping | assess uses a real `probe` | probe on Ubuntu with backend present and absent | probe timeout → `UNKNOWN` | Stage 03 compatibility report |
| FR-006 | compat | 03 | promotion only with a succeeded attempt | promotion after a restore through the API | real checkpoint and restore of a test workload | backend version change demotes | Stage 03 report; Stage 09 recovery log |
| FR-007 | supervisor | 05 | instance record construction | launch via the backend adapter | PID and start time match `/proc` | launch failure path | Stage 05 test log |
| FR-008 | supervisor | 05 | exit classification | pidfd exit detection | 4 exit classes within 2 s | daemon restart → `LOST` detection | Stage 05 timing log |
| FR-009 | supervisor | 05 | escalation timer | SIGTERM → SIGKILL path | SIGTERM-ignoring workload; unrelated process survives | PID-reuse simulation | Stage 05 test log |
| FR-010 | ckpt | 03 | operation creation | request → publish through the API | real checkpoint on Ubuntu | backend error → `CHECKPOINT_FAILED` | Stage 03 transcript |
| FR-011 | ckpt | 03 | default `leave_running` option | — | counter keeps increasing after checkpoint | — | Stage 02 matrix; Stage 03 transcript |
| FR-012 | storage | 04 | history projection | `checkpoint.list` (06) | — | — | Stage 04 test log |
| FR-013 | scheduler | 07 | interval computation with monotonic clock | scheduler → ckpt manager | 30 min run with 5 min interval | missed interval after sleep | Stage 07 timing log |
| FR-014 | scheduler | 07 | merging duplicates, priority upgrade | 10 concurrent requests → 1 operation | — | — | Stage 07 test log |
| FR-015 | scheduler | 07 | global bound | 3 due workloads | 3 real workloads, timestamps show the bound held | — | Stage 07 timing log |
| FR-016 | scheduler | 07 | priority order | — | — | — | Stage 07 test log |
| FR-017 | power | 08 | sysfs parser with 0, 1, 2 batteries | provider → scheduler | record real VM sysfs tree | garbled values dropped | Stage 08 report with VM sysfs listing |
| FR-018 | power | 08 | threshold and hysteresis state machine (simulated) | simulated discharge → scheduler | — | jitter around the threshold | Stage 08 test log (labeled simulated) |
| FR-019 | power | 08 | absent-battery capability | periodic continues with no battery | VM capability report | — | Stage 08 report |
| FR-020 | power | 08 | budget calculation | fake logind on a private bus | real inhibitor listed; real `InhibitDelayMaxUSec` | budget overrun → inhibitor released | Stage 08 report |
| FR-021 | power | 08 | re-acquire logic | simulated resume | real resume, only if VM suspend is approved | inhibitor acquisition failure | Stage 08 report |
| FR-022 | recovery | 09 | reconciliation rules | startup with seeded catalog | cold VM reboot lists the candidate | orphaned and missing artifacts | Stage 09 cold-boot record |
| FR-023 | recovery | 09 | `confirm` required | IPC restore without `confirm` refused | — | — | Stage 09 test log |
| FR-024 | recovery | 09 | once per checkpoint per boot | auto policy at startup | reboot with auto-recover on | second daemon start in same boot | Stage 09 test log |
| FR-025 | recovery | 09 | constraint and lock logic | concurrent restore requests | restore while original is alive refused | race between restore and start | Stage 09 test log |
| FR-026 | recovery | 09 | verification window | restore → verify | restored process alive for 10 s | process dies inside window → `VERIFY_FAILED` | Stage 09 report |
| FR-027 | recovery | 09 | checkpoint state unchanged on failure | injected restore failure | — | backend restore error | Stage 09 test log |
| FR-028 | supervisor | 05 | origin labeling | restart vs restore records | — | — | Stage 05 test log; Stage 10 UI review |
| FR-029 | daemon | 06 | — | — | user unit stays active with no clients | — | Stage 06 `systemctl --user` transcript |
| FR-030 | daemon | 06 | — | — | `systemctl --user restart` leaves workloads alive | `kill -9` of the daemon; catalog integrity check | Stage 06 report (depends on OD-03) |
| FR-031 | gui | 10 | view models | GUI ↔ real daemon | — | daemon unavailable handling | Stage 10 demo recording or transcript |
| FR-032 | cli | 06 | argument parsing | every method via CLI | CLI against real daemon | nonzero exit on error | Stage 06 test log (developer CLI in Stage 03) |
| FR-033 | events | 06 | event construction | every terminal state emits an event | — | catalog unavailable → buffered | Stage 06 test log |
| FR-034 | compat | 03 | reason code → message table complete | — | — | — | Stage 03 test log; Stage 10 UI review |
| NFR-001 | all | 00-12 | mypy and Ruff in every run | — | Ubuntu run each stage | — | each stage's Ubuntu log |
| NFR-002 | all | 00-12 | — | — | — | — | ADR list vs `pyproject.toml` review each stage |
| NFR-003 | events | 06 | log line format | daemon log parses as JSON | journald entries | — | Stage 06 test log |
| NFR-004 | all | 03-08 | portable unit tests on macOS and Ubuntu | — | — | — | stage logs from both hosts |
| NFR-005 | gui | 10 | import-boundary check | — | — | — | Stage 10 test log |
| NFR-006 | docs | 00-12 | — | — | — | — | presence of `docs/phases/STAGE-NN-report.md` |
| NFR-007 | daemon | 12 | — | — | — | — | Stage 12 measurement report with raw samples |
| NFR-008 | storage | 04 | timestamp and duration helpers | — | — | — | Stage 04 schema review |
| PLT-001 | docs | 00-12 | — | — | os-release and `uname -m` in every Ubuntu log | — | each stage's Ubuntu log |
| PLT-002 | all | 00 | — | — | Stage 00 baseline script on Python 3.12.3 | — | Stage 00 report (user-reported PASS) |
| PLT-003 | daemon | 06 | — | — | start without a user manager | missing user manager | Stage 06 report |
| PLT-004 | power | 08 | capability degradation | simulated bus without logind | — | logind unreachable | Stage 08 test log |
| PLT-005 | power | 08 | — | — | runs as an ordinary user | — | Stage 08 code review note |
| PLT-006 | backend | 03 | capability model | — | `daemon.capabilities` on Ubuntu | tool missing | Stage 03 report |
| PLT-007 | storage | 04 | filesystem-type check | — | ext4 accepted on VM | tmpfs refused | Stage 04 test log |
| PLT-008 | docs | 12 | — | — | — | — | Stage 12 support matrix review |
| PLT-009 | backend | 02 | — | — | engine self-check output recorded | self-check failure | Stage 02 feasibility report |
| SEC-001 | ipc | 06 | path and mode checks | — | `stat` of runtime directory and socket | pre-created insecure directory | Stage 06 test log |
| SEC-002 | ipc | 06 | UID comparison | rejected connection is audited | connection from second local user | root client rejected | Stage 06 report (needs a second test user, OD-12) |
| SEC-003 | supervisor | 05 | identity triple check | pidfd signalling | real pidfd on Ubuntu kernel | PID reuse simulation | Stage 05 test log |
| SEC-004 | supervisor | 05 | no-shell lint test | literal metacharacters in arguments | — | injection strings | Stage 05 test log; code review |
| SEC-005 | ipc | 06 | schema validators | malformed frames | — | fuzzed frames, oversize frames | Stage 06 test log |
| SEC-006 | storage | 04 | mode and owner checks | restore precheck refusal | real `chmod` on artifacts | insecure artifact | Stage 04/09 test logs |
| SEC-007 | recovery | 09 | digest and provenance check | foreign directory never offered | — | edited manifest | Stage 09 test log |
| SEC-008 | registry | 05 | environment allow-list | — | — | — | Stage 05 test log; registration warning review |
| SEC-009 | events | 06 | security event coverage | each audited action emits an event | — | — | Stage 06 test log |
| SEC-010 | daemon | 06 | — | — | `CapEff` zero, non-root UID | — | Stage 06 report; Stage 02 CRIU privilege ADR if CRIU is chosen |
| SEC-011 | storage | 04 | manifest path validation | — | — | `..`, absolute paths, symlink artifacts | Stage 04 test log |
| REL-001 | storage | 04 | state rules | publication flow | — | fault injection at each step | Stage 04 fault-injection log |
| REL-002 | storage | 04 | fsync ordering via a tracing filesystem shim | real publication on Ubuntu | — | — | Stage 04 test log |
| REL-003 | storage | 04 | pragma verification | — | — | `kill -9` during a transaction | Stage 04 test log |
| REL-004 | storage | 04 | retention floor | previous checkpoint untouched | — | crash during new checkpoint | Stage 04 test log |
| REL-005 | storage | 04 | reconciliation table | seeded inconsistencies | — | every row of the crash table | Stage 04 test log |
| REL-006 | storage | 04 | hash verification | restore precheck | — | single-byte corruption | Stage 04/09 test logs |
| REL-007 | storage | 11 | — | — | VM hard power-off during checkpoint, 3 trials | power-off mid-publication | Stage 11 cold-power-off record (earlier run in Stage 09) |
| REL-008 | ckpt | 03 | deadline handling | backend hang stub | real backend timeout | injected hang | Stage 03 test log |
| REL-009 | daemon | 06 | — | startup marks operations `INTERRUPTED` | `kill -9` of the daemon during a checkpoint | same | Stage 06 test log |
| REL-010 | storage | 04 | estimate and quota math | preflight refusal | small loop-mounted filesystem, if approved | ENOSPC injection | Stage 04 test log |
| REL-011 | storage | 04 | retention selection | expire → delete flow | — | — | Stage 04 test log |
| PRF-001 | ckpt | 02 | — | — | measured per trial | — | Stage 02 raw data |
| PRF-002 | storage | 02 | — | — | measured per trial | — | Stage 02 raw data |
| PRF-003 | recovery | 02 | — | — | measured per trial | — | Stage 02 raw data; Stage 09 attempts |
| PRF-004 | recovery | 09 | — | — | progress lost per cold-boot trial | — | Stage 02 and Stage 09 reports |
| PRF-005 | ckpt | 12 | — | — | runtime with and without checkpointing, 5 or more runs | — | Stage 12 benchmark report |
| PRF-006 | power | 08 | budget accounting | pre-suspend batch records | real inhibitor budget | budget overrun | Stage 08 report |
| PRF-007 | docs | 12 | — | — | — | — | Stage 12 benchmark methodology review |
| CMP-001 | backend | 02 | — | — | 7 workload classes × backends tried | expected-unsupported classes recorded | Stage 02 compatibility matrix |
| CMP-002 | docs | 02-12 | — | — | — | — | claim review against the matrix each stage |
| CMP-003 | compat | 03 | acknowledgement rule | IPC policy enable refused without acknowledgement (06) | — | — | Stage 03/06 test logs |
| CMP-004 | storage | 04 | version recorded and compared | restore precheck | — | changed version | Stage 04/09 test logs |
| CMP-005 | recovery | 09 | architecture comparison | — | — | foreign-architecture manifest | Stage 09 test log |
| CMP-006 | compat | 03 | one rule test per Stage 02 negative finding | — | — | — | Stage 03 test log |
| LIM-001 | gui | 10 | age computation | — | — | — | Stage 10 UI review |
| LIM-002 | gui | 10 | — | — | — | — | Stage 10 dialog review; demo |
| LIM-003 | compat | 02 | — | — | TCP class measured | — | Stage 02 matrix |
| LIM-004 | compat | 03 | device-node rule | — | — | — | Stage 03 test log |
| LIM-005 | docs | 08 | — | — | — | — | Stage 08 documentation review |
| LIM-006 | supervisor | 09 | — | instance record after restore | PID observed after restore | — | Stage 09 report |
| LIM-007 | recovery | 02 | — | — | file-modification trial | changed file before restore | Stage 02 matrix; Stage 09 test log |
| UX-001 | gui | 10 | — | state updates within 1 s | — | — | Stage 10 test log |
| UX-002 | gui | 10 | dialog model | cancel sends nothing | — | — | Stage 10 test log; UI review |
| UX-003 | gui | 10 | protected-status function | — | — | — | Stage 10 test log |
| UX-004 | gui | 10 | — | — | close GUI, workloads continue | — | Stage 10 demo |
| UX-005 | power | 08 | `simulated` flag on events | badge in GUI (10) | — | — | Stage 08 test log |
| UX-006 | ipc | 06 | every code has a message and action | — | — | — | Stage 06 test log |
| PKG-001 | packaging | 11 | — | — | clean VM snapshot install | — | Stage 11 install transcript |
| PKG-002 | packaging | 11 | — | — | `systemctl --user enable --now napguard` | — | Stage 11 transcript |
| PKG-003 | packaging | 11 | — | — | launcher opens the GUI | — | Stage 11 demo |
| PKG-004 | packaging | 11 | — | — | uninstall and purge leave no residue | — | Stage 11 transcript |
| PKG-005 | backend | 03 | guidance text | — | daemon starts with backend absent | backend missing | Stage 03/06 test logs |
| PKG-006 | storage | 04 | XDG resolution | — | — | — | Stage 04 test log |
| EXC-001 | docs | 00-12 | — | — | — | — | claim review each stage |
| EXC-002 | supervisor | 05 | setuid refusal | — | — | arbitrary-PID request impossible | Stage 05 test log |
| EXC-003 | docs | 00-12 | — | — | — | — | covered by LIM-004 evidence |
| EXC-004 | docs | 00-12 | — | — | — | — | covered by LIM-003 evidence |
| EXC-005 | docs | 08 | — | — | — | — | Stage 08 review |
| EXC-006 | docs | 00-12 | — | — | — | — | every checkpoint comes from a backend image; review each stage |
| EXC-007 | docs | 00-12 | — | — | — | — | review |
| EXC-008 | backend | 03 | contract has no attach method | — | — | — | Stage 03 API review |
| EXC-009 | docs | 08 | — | — | — | — | Stage 08 report labeling review |
| EXC-010 | recovery | 09 | machine and architecture checks | — | — | foreign machine ID | Stage 09 test log |
