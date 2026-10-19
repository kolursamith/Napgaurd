# Power and Sleep Design

- Status: Stage 01 design, implemented in Stage 08. Behaviour on the UTM VM and on physical hardware is **unverified**.
- Related: [components](components.md), [requirements](../requirements/requirements.md)

## Battery information from `power_supply` (FR-017)

The real provider reads `/sys/class/power_supply/*/` read-only, without root (PLT-005):

| Attribute | Use |
|---|---|
| `type` | `Battery` marks a battery. `Mains` or `USB` mark an external supply. Other types are ignored. |
| `present` | batteries with `present = 0` are ignored |
| `status` | `Charging`, `Discharging`, `Full`, `Not charging`, or `Unknown` |
| `capacity` | percentage, used when the energy or charge values are missing |
| `energy_now` / `energy_full` (µWh), or `charge_now` / `charge_full` (µAh) | preferred over `capacity` for aggregating several batteries |
| `online` (Mains/USB) | whether external power is connected |

- **Aggregate capacity:** the sum of `*_now` divided by the sum of `*_full` across present batteries. If any battery lacks those values, the average of the `capacity` values is used instead.
- **Discharging:** true when any battery reports `Discharging` and no external supply is online.
- **Sampling:** every 60 s while discharging and every 300 s otherwise. Unreadable or out-of-range values (capacity outside 0–100, or a non-integer) drop that sample and log a `power.sample_invalid` event.
- **Parsing:** the parser takes a directory root as a parameter, so tests can point it at captured sysfs trees.

UPower is not required in v1: sysfs is sufficient and avoids a dependency (NFR-002). This can be revisited by ADR in Stage 08.

## No battery exposed (FR-019)

UTM ARM64 VMs typically expose no battery, though this is unverified for this VM. When no present `Battery` entry exists:

- `daemon.capabilities.battery = "absent"`.
- Battery triggers are inactive, and the GUI shows "Battery triggers unavailable on this system".
- Periodic and pre-suspend checkpoints are unaffected.
- The battery is rechecked every 300 s, because a battery might appear later.

## Thresholds and hysteresis (FR-018)

Per-workload policy fields: `battery_low_pct` L (default 20), `battery_critical_pct` C (default 10), and `battery_hysteresis_pct` H (default 5).

```text
armed_low      := true at start, and whenever capacity >= L + H or external power comes online
on sample while discharging:
  if armed_low  and capacity <= L: request LOW_BATTERY checkpoint;      armed_low := false
  if armed_crit and capacity <= C: request CRITICAL_BATTERY checkpoint; armed_crit := false
armed_crit     := true whenever capacity >= C + H or external power comes online
```

- Each trigger fires at most once per discharge episode.
- A minimum gap of 5 min between battery-triggered checkpoints of the same workload prevents a storm when readings jitter.
- The requests go through the scheduler, so they are merged with other requests and ordered by priority (FR-014, FR-016).

## Periodic fallback

- Periodic checkpoints (FR-013) are the baseline protection and do not depend on battery or sleep events.
- On a system with no battery and no working sleep integration, periodic checkpoints are the only protection, and the GUI says so.
- The interval is measured from the start of the previous successful checkpoint, using the monotonic clock.

## systemd-logind `PrepareForSleep` (FR-020, FR-021)

These interfaces are documented by logind. Their behaviour on this VM must be verified in Stage 08.

- **Signal:** `org.freedesktop.login1.Manager.PrepareForSleep(b start)` on the system bus. `start=true` means suspend or hibernate is about to happen; `start=false` means the system has resumed.
- **Inhibitor:** NapGuard takes a delay inhibitor with `Manager.Inhibit("sleep", "NapGuard", "Saving process checkpoints before sleep", "delay")`. The call returns a file descriptor, and the lock lasts as long as that fd is open.
- **Budget:** logind waits for delay inhibitors only up to `InhibitDelayMaxUSec` (a manager property, 5 s by default in systemd). NapGuard reads this property at startup and after resume. It does not change `logind.conf`; that is a system configuration change needing approval (AGENTS rule 9).

### Lifetime of the delay inhibitor

```text
awake:            hold inhibitor fd (acquired at SERVING, re-acquired after resume)
PrepareForSleep(true) at t0:
                  budget := InhibitDelayMaxUSec - safety_margin (default 1 s; minimum budget 0)
                  ask scheduler for PRE_SUSPEND checkpoints with deadline t0 + budget
                  at completion of all OR at t0 + budget, whichever first: close(fd)
suspended:        nothing runs
PrepareForSleep(false):
                  re-acquire inhibitor within 2 s; reconcile operations; resume timers
```

- The inhibitor is closed exactly once per sleep cycle, and always by the deadline, even if checkpoints are still running. NapGuard must never stretch the suspend delay beyond the budget.
- NapGuard never takes a `block` inhibitor, so it can never prevent suspend.
- If the inhibitor cannot be acquired, NapGuard reports `sleep: unavailable (inhibitor)` and keeps working without pre-suspend checkpoints.

## Pre-suspend time limits and selection

- **Candidates:** workloads that are protected, have `pre_suspend = true`, and are running.
- **Ordering:** by policy priority, then by age of the newest checkpoint (oldest first).
- **Skipping:** with the default concurrency of 1, a workload is skipped if its estimated duration (the median of its last 5 checkpoint durations) exceeds the remaining budget. The skip is recorded (PRF-006). A workload with no history is attempted only if it is the first in the batch.
- With a 4 s budget, typically zero or one workloads can be checkpointed. This is expected, and is why periodic checkpoints are the primary protection.

## When a checkpoint cannot finish before suspend

1. The inhibitor is released at the deadline regardless.
2. The running operation is cancelled if the backend supports cancellation (`CancelOutcome.CANCELLED`), and its staging directory is removed.
3. If cancellation is not supported, the operation is left alone. The kernel freezes the backend and the workload together. After resume, the operation continues against its normal deadline and is validated and published only if it completes correctly. If it does not, it ends `TIMED_OUT` or `FAILED`.
4. In every case **the previous published checkpoint stays the recovery point** (REL-004). An incomplete pre-suspend checkpoint is never advertised (REL-001).
5. Each skipped, cancelled, or late operation is recorded with the reason `SLEEP_BUDGET_EXCEEDED`.

## Why userspace cannot run during actual suspend (LIM-005, EXC-005)

On suspend-to-RAM, and also on s2idle, the kernel freezes all user-space tasks (the process freezer) before suspending devices. The CPU then stops or idles in a low-power state. No timer, poll, or signal handler in NapGuard runs until the system resumes.

So NapGuard cannot watch the battery or react to power loss while suspended. If the battery runs out during suspend, the newest checkpoint persisted before suspend is the recovery point after the next boot. Hibernation (suspend-to-disk) is the kernel's mechanism, not NapGuard's. NapGuard does not implement it or depend on it.

If the lid closes but Linux stays awake (the suspend policy is "ignore", or suspend fails), NapGuard keeps running normally. A lid closure is not treated as a suspend event; only `PrepareForSleep` is.

## Testing with simulated inputs (UX-005, EXC-009)

| Test level | What is real | What is simulated | How it is labeled |
|---|---|---|---|
| Unit | policy logic | sysfs trees in a temporary directory, from fixtures captured from real devices where available | test names contain `simulated`; `simulated=true` on events |
| Integration | NapGuard D-Bus client code | a private `dbus-daemon --session` with a scripted fake `org.freedesktop.login1` service that emits `PrepareForSleep` and provides `Inhibit` | `simulated=true`; the report says "simulated logind" |
| Linux system (VM) | the real logind: inhibitor acquired and listed in `systemd-inhibit --list`, real `InhibitDelayMaxUSec` read, real VM sysfs contents recorded | battery transitions (the VM has no battery, unverified) | report: "VM, battery simulated" |
| Real suspend (optional, needs approval because it suspends the VM) | `systemctl suspend` on the VM, if UTM supports it | — | report: "VM suspend", not "hardware" |
| Physical laptop (outside the current scope) | a real battery and lid | — | only this level may be reported as hardware-validated |

A simulated provider is selected by explicit configuration (`power.provider = "simulated"`) and never automatically. When it is active, `daemon.capabilities` reports it.
