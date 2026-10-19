"""Stage 02 trial orchestration: fresh control, launch, checkpoint, kill, restore, verify.

A trial writes everything to one run directory:

    <run_root>/<run_id>/
        result.json            checks, facts, environment, git revision, artifacts
        commands.jsonl         every command: argv, exit code, duration, log paths
        events.jsonl           harness events (marks, adoptions, terminations)
        logs/                  stdout/stderr of every command and backend logs
        reports/fresh.txt      NGSTATE observations of the fresh-start control
        reports/managed.txt    NGSTATE observations of the managed workload
        checkpoint/            native backend artifacts (kept for evidence)
        bin/                   the workload binary built for this run
        reboot-manifest.json   (reboot-prepare only) state for reboot-resume
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import model, verify
from .backends import Backend, BackendStepError, Criu, Dmtcp, Session
from .results import Check, Status, overall
from .runner import (
    PERSISTENT_FS,
    ArtifactSet,
    Recorder,
    alive,
    boot_id,
    boot_ticks_now,
    collect_artifacts,
    current_listeners,
    environment,
    git_revision,
    listener_violations,
    running_group_members,
    sha256_file,
    utc_now,
    wait_until,
)

HERE = Path(__file__).resolve().parent.parent  # experiments/stage02
REPO = HERE.parent.parent
TICK_MS = 10


@dataclass(frozen=True)
class WorkloadClass:
    name: str
    kind: str  # "c", "py", or "none" (not implemented)
    args: tuple[str, ...]
    roles: tuple[str, ...]
    ballast_mib: int = 0
    report_every: int = 10
    note: str = ""


CLASSES: dict[str, WorkloadClass] = {
    c.name: c
    for c in [
        WorkloadClass("c-single", "c", ("--ballast-mib", "4"), ("main",), ballast_mib=4,
                      note="single-threaded C with 4 MiB of nonce-derived memory"),
        WorkloadClass("c-threads", "c", ("--threads", "3"), ("main", "t0", "t1", "t2"),
                      note="multi-threaded C (3 worker threads)"),
        WorkloadClass("c-child", "c", ("--fork-child",), ("main", "child"),
                      note="parent with one child process"),
        WorkloadClass("c-file-static", "c", ("--hold-file", "{run}/held.txt"), ("main",),
                      note="holds a regular file open for writing; file unchanged after dump"),
        WorkloadClass("c-file-grow", "c", ("--hold-file", "{run}/held.txt", "--hold-file-grow"),
                      ("main",), note="holds a regular file open and keeps appending (LIM-007)"),
        WorkloadClass("py", "py", ("--ballast-mib", "1"), ("main",), ballast_mib=1,
                      report_every=20, note="Python computation with 1 MiB ballast"),
        WorkloadClass("tcp", "none", (), (), note="TCP connection class: not implemented in the "
                      "Stage 02 harness yet; expected limitation (LIM-003)"),
        WorkloadClass("gui", "none", (), (), note="GUI application class: not implemented in the "
                      "Stage 02 harness; expected unsupported"),
    ]
}


@dataclass
class Options:
    backend: str
    workload_class: str
    run_root: Path
    dmtcp_prefix: Path | None = None
    criu_bin: Path | None = None
    criu_mode: str = "unprivileged"
    run_on_s: float = 4.0
    observe_s: float = 3.0


@dataclass
class TrialState:
    run_dir: Path
    rec: Recorder
    checks: list[Check] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    sessions: list[Session] = field(default_factory=list)

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        self.rec.event("check", name=check.name, status=str(check.status), detail=check.detail)
        return check


# ------------------------------------------------------------------ helpers


def new_run_dir(run_root: Path, label: str) -> Path:
    run_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{label}-{secrets.token_hex(2)}"
    run_dir = run_root / run_id
    run_dir.mkdir(parents=True, mode=0o700)
    (run_dir / "reports").mkdir(mode=0o700)
    return run_dir


def make_backend(opts: Options, rec: Recorder) -> Backend:
    if opts.backend == "dmtcp":
        return Dmtcp(rec, opts.dmtcp_prefix)
    if opts.backend == "criu":
        return Criu(rec, opts.criu_bin, opts.criu_mode)
    raise ValueError(f"unknown backend {opts.backend!r}")


def build_workload(state: TrialState) -> Check:
    if not shutil.which("cc"):
        return Check("workload built", Status.BLOCKED, "no C compiler (install build-essential)")
    out = state.run_dir / "bin" / "ngcounter"
    out.parent.mkdir(mode=0o700, exist_ok=True)
    src = HERE / "workload" / "ngcounter.c"
    res = state.rec.run("build-workload", ["cc", "-O2", "-Wall", "-Wextra", "-pthread",
                                           "-o", str(out), str(src)], timeout=120)
    if not res.ok:
        return Check("workload built", Status.FAIL, res.summary())
    return Check("workload built", Status.PASS, f"{out}",
                 facts={"binary": str(out), "sha256": sha256_file(out),
                        "source_sha256": sha256_file(src)})


def workload_argv(state: TrialState, wc: WorkloadClass, report: Path, label: str) -> list[str]:
    args = [a.replace("{run}", str(state.run_dir)) for a in wc.args]
    common = ["--report", str(report), "--label", label, "--tick-ms", str(TICK_MS),
              "--report-every", str(wc.report_every), *args]
    if wc.kind == "c":
        return [str(state.run_dir / "bin" / "ngcounter"), *common]
    return [sys.executable, str(HERE / "workload" / "ngcounter.py"), *common]


def by_role(observations: Sequence[model.Observation]) -> dict[str, list[model.Observation]]:
    grouped: dict[str, list[model.Observation]] = {}
    for obs in observations:
        grouped.setdefault(obs.role, []).append(obs)
    return grouped


def wait_for_roles(path: Path, roles: Sequence[str], start_line: int, count: int,
                   timeout: float) -> bool:
    def ready() -> bool:
        grouped = by_role(model.read_reports(path, start_line))
        return all(len(grouped.get(r, [])) >= count for r in roles)

    return wait_until(ready, timeout=timeout, interval=0.1)


def max_counters(observations: Sequence[model.Observation]) -> dict[str, int]:
    return {role: max(o.counter for o in obs) for role, obs in by_role(observations).items()}


def min_counters(observations: Sequence[model.Observation]) -> dict[str, int]:
    return {role: min(o.counter for o in obs) for role, obs in by_role(observations).items()}


def adopt_reported(state: TrialState, backend: Backend, observations: Sequence[model.Observation],
                   prefix: str, not_before_ticks: int) -> None:
    seen: set[int] = set()
    for obs in observations:
        pid = backend.restored_pid(obs.kpid, obs.pid)
        if pid is not None and pid not in seen:
            seen.add(pid)
            state.rec.adopt(f"{prefix}-{obs.role}", pid, not_before_ticks)


def write_result(state: TrialState, opts: Options, kind: str) -> Status:
    status = overall(state.checks)
    result = {
        "kind": kind,
        "status": str(status),
        "finished_at": utc_now(),
        "backend": opts.backend,
        "criu_mode": opts.criu_mode if opts.backend == "criu" else None,
        "workload_class": opts.workload_class,
        "run_dir": str(state.run_dir),
        "git": git_revision(REPO),
        "environment": environment(opts.run_root),
        "checks": [c.to_json() for c in state.checks],
        "facts": state.facts,
    }
    (state.run_dir / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True,
                                                          default=str), encoding="utf-8")
    print_summary(state, status)
    return status


def print_summary(state: TrialState, status: Status) -> None:
    print("\n### Result")
    for check in state.checks:
        flag = "" if check.mandatory else " (informational)"
        print(f"{check.status:<7}  {check.name}{flag} — {check.detail}")
    print(f"\nTRIAL VERDICT: {status}   evidence: {state.run_dir}")


# ------------------------------------------------------------------- phases


def fresh_control(state: TrialState, wc: WorkloadClass) -> list[model.Observation]:
    report = state.run_dir / "reports" / "fresh.txt"
    proc = state.rec.spawn("fresh-control", workload_argv(state, wc, report, "fresh"),
                           capture=False)
    wait_for_roles(report, ["main"], 0, 3, timeout=15)
    state.rec.terminate(proc)
    return model.read_reports(report)


def launch_and_checkpoint(state: TrialState, opts: Options, wc: WorkloadClass,
                          backend: Backend) -> tuple[ArtifactSet, dict[str, Any]] | None:
    """Launch under the backend, checkpoint, run on, terminate. None if a step failed."""
    report = state.run_dir / "reports" / "managed.txt"
    launched_at = boot_ticks_now()
    session = backend.launch(workload_argv(state, wc, report, "managed"))
    state.sessions.append(session)
    guard_network(state, "launch")
    if not wait_for_roles(report, wc.roles, 0, 3, timeout=30):
        state.add(Check("managed workload running", Status.FAIL,
                        "no observations from every role within 30 s"))
        return None
    started = model.read_reports(report)
    adopt_reported(state, backend, started, "original", launched_at)
    state.add(Check("managed workload running", Status.PASS,
                    f"roles {list(wc.roles)} reporting; nonce {started[0].nonce}",
                    facts={"nonce": started[0].nonce, "pid": started[0].pid,
                           "kpid": started[0].kpid}))

    before = model.read_reports(report)
    lower = max_counters(before)
    request_line = model.count_lines(report)
    state.rec.event("mark_ckpt_request", lower=lower, line=request_line)
    ckpt_check, artifacts = backend.checkpoint(session)
    state.add(ckpt_check)
    guard_network(state, "checkpoint")
    if ckpt_check.status is not Status.PASS:
        return None
    done_line = model.count_lines(report)
    if not wait_for_roles(report, wc.roles, done_line, 1, timeout=15):
        state.add(Check("workload continued after checkpoint", Status.FAIL,
                        "no new observations within 15 s of the checkpoint (FR-011)"))
        return None
    upper = min_counters(model.read_reports(report, done_line))
    state.add(Check("workload continued after checkpoint", Status.PASS,
                    f"first post-checkpoint counters {upper} (FR-011)"))

    time.sleep(opts.run_on_s)
    original_dead = backend.stop_original(session)
    for tracked in state.rec.tracked:
        if tracked.name.startswith("original-"):  # e.g. a forked child, adopted by kernel PID
            original_dead = state.rec.terminate(tracked, sig=signal.SIGKILL) and original_dead
    time.sleep(0.3)  # every original process is gone: nothing below can be post-kill output
    original = model.read_reports(report)
    kill = max_counters(original)
    kill_line = model.count_lines(report)
    state.add(Check("original terminated", Status.PASS if original_dead else Status.FAIL,
                    f"counters at termination {kill}"))

    fs_ok = artifacts.fs in PERSISTENT_FS
    state.add(Check("artifacts on persistent filesystem", Status.PASS if fs_ok else Status.FAIL,
                    f"{artifacts.directory} on {artifacts.fs} ({artifacts.device})"))
    marks = {"lower": lower, "upper": upper, "kill": kill, "kill_line": kill_line,
             "nonce": original[0].nonce, "original_dead": original_dead}
    state.facts["marks"] = marks
    state.facts["artifacts"] = [asdict(a) for a in artifacts.items]
    state.facts["artifact_total_bytes"] = artifacts.total_bytes
    state.facts["artifact_fs"] = {"fs": artifacts.fs, "device": artifacts.device}
    return (artifacts, marks) if fs_ok and original_dead else None


def restore_and_verify(state: TrialState, opts: Options, wc: WorkloadClass, backend: Backend,
                       artifacts: ArtifactSet, marks: dict[str, Any]) -> None:
    report = state.run_dir / "reports" / "managed.txt"
    kill_line = int(marks["kill_line"])
    original = [o for o in model.read_reports(report) if o.line_no <= kill_line]
    started = time.monotonic()
    restore_at = boot_ticks_now()
    restore_check, session = backend.restore(backend.image_paths(artifacts), restore_at)
    state.add(restore_check)
    if session is not None:
        state.sessions.append(session)
    if restore_check.status is not Status.PASS:
        return
    if not wait_for_roles(report, wc.roles, kill_line, 3, timeout=30):
        restored = model.read_reports(report, kill_line)
        state.add(Check("restored workload reporting", Status.FAIL,
                        f"{len(restored)} observations within 30 s of restore"))
        return
    adopt_reported(state, backend, model.read_reports(report, kill_line), "restored", restore_at)
    guard_network(state, "restore")
    time.sleep(opts.observe_s)
    restored = model.read_reports(report, kill_line)
    state.facts["restore_to_first_report_s"] = round(time.monotonic() - started - opts.observe_s, 3)
    for role in wc.roles:
        state.add(verify.verify_restore(
            role=role, original=original,
            marks=verify.Marks(int(marks["lower"][role]), int(marks["upper"][role]),
                               int(marks["kill"][role])),
            restored=restored, original_dead=bool(marks["original_dead"]),
            ballast_mib=wc.ballast_mib,
        ))


def finish(state: TrialState, backend: Backend) -> None:
    backend.cleanup(state.sessions)
    survivors = state.rec.cleanup()
    state.add(Check("experiment processes cleaned up",
                    Status.PASS if not survivors else Status.FAIL,
                    "all tracked processes gone" if not survivors else f"still alive: {survivors}"))


def guard_network(state: TrialState, step: str) -> None:
    """Abort the trial if any experiment process listens beyond loopback (OD-13 guard)."""
    pids: set[int] = set()
    for tracked in state.rec.tracked:
        if tracked.own_group and tracked.popen is not None and tracked.popen.returncode is None:
            pids.update(m.ident.pid for m in running_group_members(tracked.ident.pid))
        elif alive(tracked.ident):
            pids.add(tracked.ident.pid)
    try:
        listeners = current_listeners()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        state.rec.cleanup()
        raise BackendStepError(f"after {step}: listener audit failed, stopping: {exc}") from exc
    exposed = listener_violations(listeners, pids)
    if exposed:
        state.rec.event("network_exposure", step=step, listeners=[e.__dict__ for e in exposed])
        state.rec.cleanup()
        raise BackendStepError(f"after {step}: experiment process listening beyond loopback: "
                               + ", ".join(f"{e.proto} {e.local} pids={e.pids}" for e in exposed))


# --------------------------------------------------------------- entrypoints


def prelude(opts: Options, label: str) -> tuple[TrialState, WorkloadClass, Backend] | Status:
    wc = CLASSES[opts.workload_class]
    run_dir = new_run_dir(opts.run_root, label)
    state = TrialState(run_dir, Recorder(run_dir))
    state.facts["class_note"] = wc.note
    if wc.kind == "none":
        state.add(Check("workload class", Status.SKIP, wc.note))
        return write_result(state, opts, "trial")
    backend = make_backend(opts, state.rec)
    for check in (build_workload(state), backend.probe()):
        state.add(check)
    state.facts["backend_version"] = backend.version
    if overall(state.checks) is not Status.PASS:
        return write_result(state, opts, "trial")
    return state, wc, backend


def run_trial(opts: Options) -> Status:
    prepared = prelude(opts, f"{opts.backend}-{opts.workload_class}")
    if isinstance(prepared, Status):
        return prepared
    state, wc, backend = prepared
    try:
        fresh = fresh_control(state, wc)
        launched = launch_and_checkpoint(state, opts, wc, backend)
        if launched is not None:
            artifacts, marks = launched
            state.add(verify.verify_fresh_control(fresh, str(marks["nonce"])))
            restore_and_verify(state, opts, wc, backend, artifacts, marks)
    except BackendStepError as exc:
        state.add(Check("backend step", Status.FAIL, str(exc)))
    finally:
        finish(state, backend)
    return write_result(state, opts, "trial")


def run_matrix(opts: Options, classes: Sequence[str]) -> Status:
    results: dict[str, Status] = {}
    for name in classes:
        print(f"\n======== {opts.backend} / {name}")
        results[name] = run_trial(Options(**{**asdict(opts), "workload_class": name}))
    print("\n### Matrix")
    for name, result in results.items():
        print(f"{result:<7}  {opts.backend} / {name} — {CLASSES[name].note}")
    return overall(Check(name, result, "") for name, result in results.items())


def reboot_prepare(opts: Options) -> Status:
    prepared = prelude(opts, f"reboot-{opts.backend}-{opts.workload_class}")
    if isinstance(prepared, Status):
        return prepared
    state, wc, backend = prepared
    try:
        fresh = fresh_control(state, wc)
        launched = launch_and_checkpoint(state, opts, wc, backend)
        if launched is not None:
            artifacts, marks = launched
            state.add(verify.verify_fresh_control(fresh, str(marks["nonce"])))
            os.sync()
            manifest = {
                "created_at": utc_now(),
                "boot_id": boot_id(),
                "options": {**asdict(opts), "run_root": str(opts.run_root),
                            "dmtcp_prefix": str(opts.dmtcp_prefix) if opts.dmtcp_prefix else None,
                            "criu_bin": str(opts.criu_bin) if opts.criu_bin else None},
                "marks": marks,
                "artifacts": [asdict(a) for a in artifacts.items],
                "artifact_dir": artifacts.directory,
                # Sessions of the pre-reboot run: a CRIU-restored tree keeps its session id.
                "sessions": sorted(state.rec.sessions),
                "workload_binary_sha256": sha256_file(state.run_dir / "bin" / "ngcounter")
                if wc.kind == "c" else None,
            }
            (state.run_dir / "reboot-manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
            os.sync()
            state.add(Check("reboot manifest written and synced", Status.PASS,
                            str(state.run_dir / "reboot-manifest.json")))
    except BackendStepError as exc:
        state.add(Check("backend step", Status.FAIL, str(exc)))
    finally:
        finish(state, backend)
    status = write_result(state, opts, "reboot-prepare")
    if status is Status.PASS:
        print("\nNEXT (only after you decide to reboot; nothing has been rebooted):")
        print("  1. Shut the VM down completely:   sudo systemctl poweroff")
        print("  2. Start it again from UTM, log in as the same user, open a terminal.")
        print(f"  3. Run: python3 experiments/stage02/run_stage02.py reboot-resume "
              f"--run-dir {state.run_dir}")
    return status


def reboot_resume(run_dir: Path) -> Status:
    manifest = json.loads((run_dir / "reboot-manifest.json").read_text(encoding="utf-8"))
    raw = manifest["options"]
    opts = Options(backend=raw["backend"], workload_class=raw["workload_class"],
                   run_root=Path(raw["run_root"]),
                   dmtcp_prefix=Path(raw["dmtcp_prefix"]) if raw["dmtcp_prefix"] else None,
                   criu_bin=Path(raw["criu_bin"]) if raw["criu_bin"] else None,
                   criu_mode=raw["criu_mode"], run_on_s=raw["run_on_s"], observe_s=raw["observe_s"])
    wc = CLASSES[opts.workload_class]
    state = TrialState(run_dir, Recorder(run_dir))
    # Restored processes may carry the pre-reboot session ids; adoption additionally
    # requires that they started after the restore command began (not_before_ticks).
    state.rec.sessions.update(int(s) for s in manifest.get("sessions", []))
    now_boot = boot_id()
    rebooted = now_boot != manifest["boot_id"]
    state.add(Check("system rebooted since checkpoint",
                    Status.PASS if rebooted else Status.BLOCKED,
                    f"boot_id {manifest['boot_id']} -> {now_boot}"))
    intact = []
    for art in manifest["artifacts"]:
        path = run_dir / art["path"]
        if art["is_symlink"]:
            intact.append(path.is_symlink())
        else:
            intact.append(path.is_file() and sha256_file(path) == art["sha256"])
    state.add(Check("checkpoint artifacts intact after reboot",
                    Status.PASS if intact and all(intact) else Status.FAIL,
                    f"{sum(intact)}/{len(intact)} artifacts match their pre-reboot SHA-256"))
    backend = make_backend(opts, state.rec)
    state.add(backend.probe())
    if overall(state.checks) is Status.PASS:
        artifacts = collect_artifacts(Path(manifest["artifact_dir"]), run_dir,
                                      ["ckpt_*.dmtcp", "dmtcp_restart_script*.sh", "*.img"])
        try:
            restore_and_verify(state, opts, wc, backend, artifacts, manifest["marks"])
        except BackendStepError as exc:
            state.add(Check("backend step", Status.FAIL, str(exc)))
        finally:
            finish(state, backend)
    return write_result(state, opts, "reboot-resume")


def dmtcp_isolation_check(run_root: Path, dmtcp_prefix: Path | None) -> Status:
    """OD-13 evidence: one loopback-confined coordinator; no workload, no checkpoint."""
    opts = Options(backend="dmtcp", workload_class="-", run_root=run_root,
                   dmtcp_prefix=dmtcp_prefix)
    run_dir = new_run_dir(run_root, "dmtcp-isolation-check")
    state = TrialState(run_dir, Recorder(run_dir))
    backend = Dmtcp(state.rec, dmtcp_prefix)
    try:
        for check in backend.isolation_check():
            state.add(check)
    finally:
        backend.cleanup([])
        survivors = state.rec.cleanup()
        state.add(Check("experiment processes cleaned up",
                        Status.PASS if not survivors else Status.FAIL,
                        "all tracked processes gone" if not survivors else f"alive: {survivors}"))
    state.facts["trial_gate"] = ("closed: DMTCP_LOOPBACK_ONLY_COORDINATOR_VERIFIED is False; "
                                 "a PASS here is evidence for OD-13, not permission to run trials")
    return write_result(state, opts, "dmtcp-isolation-check")
