"""Preparation for further Stage 02 workload classes (no checkpoint is run here).

ADR 0004 authorises only `c-single`. These tests prove the harness is ready for the
proposed Amendment 2 classes (c-threads, py, c-file-static, c-file-grow) and still
refuses them today, and that evidence is summarised without inventing values.
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from ngexp import backends, cli, model, runner, trial, verify
from ngexp.backends import Session
from ngexp.results import Check, Status

NONCE = "b" * 32
AMENDMENT_2 = ("c-threads", "py", "c-file-static", "c-file-grow")
linux_only = pytest.mark.skipif(platform.system() != "Linux", reason="needs Linux /proc")


def ngstate(counter: int, nonce: str = NONCE) -> str:
    digest = model.digest_at(nonce, "main", counter)
    return (f"NGSTATE v1 run=managed role=main pid=1 kpid=1 nonce={nonce} counter={counter} "
            f"digest={digest:016x} ballast=- mono_ns={counter}")


def snap(size: int, sha: str) -> dict[str, object]:
    return {"exists": True, "size": size, "sha256": sha}


# ------------------------------------------------------------------ ADR 0004 scope


def seed_launched_run(run_root: Path, cls: str, stamp: str = "20261020T000000Z") -> Path:
    """A run directory that launched a DMTCP workload (as the first, failed py trial did)."""
    run_dir = run_root / f"{stamp}-dmtcp-{cls}-0000"
    run_dir.mkdir(parents=True)
    (run_dir / "commands.jsonl").write_text(
        json.dumps({"kind": "spawn", "name": "dmtcp-launch"}) + "\n", encoding="utf-8")
    return run_dir


@pytest.mark.parametrize("cls", AMENDMENT_2)
def test_amendment_2_classes_are_allowed_for_same_boot_trial_only(tmp_path: Path,
                                                                 cls: str) -> None:
    assert cls not in backends.CONTROLLED_TRIAL_CLASSES  # not part of the base exception
    if cls in backends.ONE_TIME_RETESTS:  # py: the preserved original run (Amendment 3)
        seed_launched_run(tmp_path, cls)
    opts = trial.Options(backend="dmtcp", workload_class=cls, run_root=tmp_path,
                         dmtcp_controlled_trial=True)
    assert trial.controlled_trial_refusal(opts, "trial") is None  # Amendment 2 (approved)
    expected = {"reboot-prepare": "not allowed under ADR 0004 for `reboot-prepare`",
                "reboot-resume": "not allowed under ADR 0004 for `reboot-resume`",
                "matrix": "not `matrix`"}
    for entry, fragment in expected.items():  # cross-reboot and matrix stay prohibited
        refusal = trial.controlled_trial_refusal(opts, entry)
        assert refusal is not None
        assert fragment in refusal
    other = trial.Options(backend="criu", workload_class=cls, run_root=tmp_path,
                          dmtcp_controlled_trial=True)
    assert trial.controlled_trial_refusal(other, "trial") is not None  # DMTCP only


def test_base_exception_and_global_gate_unchanged() -> None:
    assert backends.CONTROLLED_TRIAL_CLASSES == ("c-single",)
    assert backends.DMTCP_LOOPBACK_ONLY_COORDINATOR_VERIFIED is False
    assert backends.FD_HANDOFF_ANALYSED_VERSION == "4.2.0"


# ---------------------------------------------------------------- held-file verifier


def test_static_held_file_must_be_byte_identical() -> None:
    header = f"NGHOLD v1 nonce={NONCE}\n"
    same = snap(len(header), "h1")
    ok = verify.verify_held_file(class_name="c-file-static", nonce=NONCE, at_checkpoint=same,
                                 at_kill=same, after_restore=same, text_after=header)
    assert ok.status is Status.PASS and ok.mandatory
    changed = verify.verify_held_file(class_name="c-file-static", nonce=NONCE, at_checkpoint=same,
                                      at_kill=same, after_restore=snap(99, "h2"),
                                      text_after=header + "junk\n")
    assert changed.status is Status.FAIL
    lost = verify.verify_held_file(class_name="c-file-static", nonce=NONCE, at_checkpoint=same,
                                   at_kill=same, after_restore=same,
                                   text_after=f"NGHOLD v1 nonce={'c' * 32}\n")
    assert lost.status is Status.FAIL


@pytest.mark.parametrize(
    ("counters", "torn", "expect"),
    [
        ([10, 20, 30, 40, 50], False, "appended after the pre-termination data"),
        ([10, 20, 30, 40, 20, 30], False, "counter rewind"),
        ([10, 20, 30, 40, 20], True, "overwriting post-checkpoint data"),
    ],
)
def test_growing_held_file_behaviour_is_recorded_not_judged(counters: list[int], torn: bool,
                                                            expect: str) -> None:
    lines = [f"NGHOLD v1 nonce={NONCE}", *(ngstate(c) for c in counters)]
    if torn:
        lines.append("NGSTATE v1 run=managed role=main pid=1 kpid=1 nonce=")  # torn write
    check = verify.verify_held_file(
        class_name="c-file-grow", nonce=NONCE, at_checkpoint=snap(100, "a"),
        at_kill=snap(200, "b"), after_restore=snap(250, "c"), text_after="\n".join(lines) + "\n")
    assert check.mandatory is False  # LIM-007: an observation, not a pass/fail gate
    assert expect in check.detail


# ------------------------------------------------------------------- evidence summary


def write_result(run_root: Path, name: str, payload: dict[str, object]) -> None:
    (run_root / name).mkdir(parents=True)
    (run_root / name / "result.json").write_text(json.dumps(payload), encoding="utf-8")


def test_summary_reports_only_recorded_values(tmp_path: Path) -> None:
    write_result(tmp_path, "20261020T100428Z-reboot-dmtcp-c-single-ed7a", {
        "kind": "reboot-resume", "status": "PASS", "backend": "dmtcp",
        "workload_class": "c-single", "git": {"head": "55905be4"},
        "facts": {"restore_to_first_report_s": 0.62},
        "checks": [{"name": "restored state continuity (main)", "status": "PASS",
                    "facts": {"nonce": NONCE, "kill_counter": 430,
                              "restored_first_counter": 40, "restored_last_counter": 340}}],
    })
    write_result(tmp_path, "20261020T093355Z-dmtcp-c-single-7bce", {
        "kind": "trial", "status": "PASS", "backend": "dmtcp", "workload_class": "c-single",
        "facts": {"artifact_total_bytes": 25059500},
        "checks": [{"name": "native checkpoint created", "status": "PASS",
                    "facts": {"duration_s": 1.21,
                              "images": [{"name": "ckpt_x.dmtcp", "size": 25059040}]}}],
    })
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "result.json").write_text("{not json", encoding="utf-8")
    rows = {r["run"]: r for r in trial.summarize_runs(tmp_path)}
    same_boot = rows["20261020T093355Z-dmtcp-c-single-7bce"]
    assert same_boot["checkpoint_s"] == 1.21 and same_boot["image_bytes"] == 25059040
    assert same_boot["restore_to_first_report_s"] is None  # absent -> None, never estimated
    resumed = rows["20261020T100428Z-reboot-dmtcp-c-single-ed7a"]
    assert (resumed["kill_counter"], resumed["restored_first_counter"]) == (430, 40)
    assert resumed["checkpoint_s"] is None and resumed["image_bytes"] is None
    assert "error" in rows["broken"]


def test_summarize_cli_is_read_only_and_runs_on_any_host(tmp_path: Path,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    write_result(tmp_path, "r1", {"kind": "trial", "status": "FAIL", "checks": [], "facts": {}})
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    assert cli.main(["--run-root", str(tmp_path), "summarize"]) == 0
    assert "r1\ttrial\tFAIL" in capsys.readouterr().out
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before
    assert cli.main(["--run-root", str(tmp_path / "empty"), "summarize"]) == 3  # SKIP: nothing


# ----------------------------------- Linux: native preconditions of the Amendment 2 classes


def native_class_run(tmp_path: Path, cls: str) -> tuple[runner.Recorder, runner.Tracked, Path]:
    rec = runner.Recorder(tmp_path / "run")
    state = trial.TrialState(rec.run_dir, rec)
    wc = trial.CLASSES[cls]
    if wc.kind == "c":
        assert trial.build_workload(state).status is Status.PASS
    report = rec.run_dir / "reports" / "native.txt"
    report.parent.mkdir(exist_ok=True)
    tracked = rec.spawn(f"native-{cls}", trial.workload_argv(state, wc, report, "native"),
                        capture=False)
    assert trial.wait_for_roles(report, wc.roles, 0, 2, timeout=20)
    return rec, tracked, report


@linux_only
@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler")
@pytest.mark.parametrize("cls", AMENDMENT_2)
def test_amendment_2_classes_meet_preconditions_natively(tmp_path: Path, cls: str) -> None:
    rec, tracked, report = native_class_run(tmp_path, cls)
    try:
        pid = tracked.ident.pid
        assert {m.ident.pid for m in runner.running_group_members(pid)} == {pid}  # one process
        assert backends.workload_socket_fds(pid) == []                             # no sockets
        observations = model.read_reports(report)
        assert verify.chain_problems(observations) == []
        held = rec.run_dir / "held.txt"
        if cls == "c-file-static":
            first = runner.sha256_file(held)
            assert trial.wait_for_roles(report, ["main"], len(observations), 3, timeout=10)
            assert runner.sha256_file(held) == first  # never written after the header
        if cls == "c-file-grow":
            size = held.stat().st_size
            assert runner.wait_until(lambda: held.stat().st_size > size, timeout=10)
    finally:
        assert rec.cleanup() == []


@linux_only
@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler")
def test_c_child_would_be_refused_by_the_single_process_precheck(tmp_path: Path) -> None:
    rec, tracked, _ = native_class_run(tmp_path, "c-child")
    try:
        members = {m.ident.pid for m in runner.running_group_members(tracked.ident.pid)}
        assert len(members) == 2  # parent + child: check_controlled_workload would refuse
    finally:
        assert rec.cleanup() == []


# ------------------------------------------------- Amendment 2 gate (disabled by default)


def test_amendment_2_is_approved_for_exactly_four_classes() -> None:
    assert backends.AMENDMENT_2_APPROVED is True  # owner approval, 2026-10-20
    assert backends.AMENDMENT_2_CLASSES == AMENDMENT_2
    assert set(backends.NEVER_UNDER_ADR_0004) == {"c-child", "tcp", "gui"}
    assert not set(backends.AMENDMENT_2_CLASSES) & set(backends.NEVER_UNDER_ADR_0004)


def test_without_approval_the_refusal_names_the_pending_amendment(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backends, "AMENDMENT_2_APPROVED", False)  # the flag is the only switch
    opts = trial.Options(backend="dmtcp", workload_class="py", run_root=tmp_path,
                         dmtcp_controlled_trial=True)
    refusal = trial.controlled_trial_refusal(opts, "trial")
    assert refusal is not None and "Amendment 2, not approved" in refusal


def test_when_approved_amendment_2_is_same_boot_trial_only(tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backends, "AMENDMENT_2_APPROVED", True)
    seed_launched_run(tmp_path, "py")  # Amendment 3 needs the preserved original py run
    for cls in AMENDMENT_2:
        opts = trial.Options(backend="dmtcp", workload_class=cls, run_root=tmp_path,
                             dmtcp_controlled_trial=True)
        assert trial.controlled_trial_refusal(opts, "trial") is None
        for entry in ("reboot-prepare", "reboot-resume", "matrix"):
            assert trial.controlled_trial_refusal(opts, entry) is not None
    for cls in ("c-child", "tcp", "gui"):  # never, even with the amendment approved
        opts = trial.Options(backend="dmtcp", workload_class=cls, run_root=tmp_path,
                             dmtcp_controlled_trial=True)
        refusal = trial.controlled_trial_refusal(opts, "trial")
        assert refusal is not None and "not allowed under ADR 0004 at all" in refusal


# ------------------------------------------------------- multi-role state continuity


def role_obs(role: str, counters: list[int], first_line: int) -> list[model.Observation]:
    return [model.Observation("managed", role, 1, 1, NONCE, c, model.digest_at(NONCE, role, c),
                              None, c, first_line + i) for i, c in enumerate(counters)]


def test_every_thread_role_must_resume_from_the_checkpoint() -> None:
    marks = verify.Marks(30, 40, 450)
    original = [o for r in ("main", "t0", "t1", "t2") for o in role_obs(r, [0, 30, 450], 1)]
    restored = [*role_obs("main", [40, 60], 100), *role_obs("t0", [40, 60], 200),
                *role_obs("t1", [450, 460], 300),            # t1 did not roll back
                *role_obs("t2", [40, 60], 400)]
    results = {role: verify.verify_restore(role=role, original=original, marks=marks,
                                           restored=restored, original_dead=True)
               for role in ("main", "t0", "t1", "t2")}
    assert [r for r, c in results.items() if c.status is Status.FAIL] == ["t1"]


# ------------------------------------------------------------- PRF-003 restore timing


def test_first_report_latency_uses_the_shared_monotonic_clock() -> None:
    obs = role_obs("main", [40, 50], 1)  # mono_ns = counter, so the first report is at 40 ns
    started = obs[0].mono_ns - 1_217_000_000
    assert verify.first_report_latency_s(started, obs) == 1.217
    assert verify.first_report_latency_s(started, []) is None
    assert verify.first_report_latency_s(obs[0].mono_ns + 1, obs) is None  # clock mismatch


def test_summary_labels_legacy_restore_timing_as_upper_bound(tmp_path: Path) -> None:
    write_result(tmp_path, "legacy", {"kind": "trial", "status": "PASS", "checks": [],
                                      "facts": {"restore_to_first_report_s": 1.217}})
    write_result(tmp_path, "precise", {"kind": "trial", "status": "PASS", "checks": [],
                                       "facts": {"restore_to_first_report_s": 0.9,
                                                 "restore_to_three_reports_upper_bound_s": 1.2,
                                                 "restore_timing_definition": "x"}})
    rows = {r["run"]: r for r in trial.summarize_runs(tmp_path)}
    assert rows["legacy"]["restore_to_first_report_s"] is None
    assert rows["legacy"]["restore_upper_bound_s"] == 1.217
    assert rows["legacy"]["restore_timing"] == "legacy upper bound"
    assert (rows["precise"]["restore_to_first_report_s"], rows["precise"]["restore_upper_bound_s"],
            rows["precise"]["restore_timing"]) == (0.9, 1.2, "precise")


# ------------------------------------------------------------ Ubuntu script allowlist

SCRIPT = Path(__file__).resolve().parents[2] / "experiments" / "stage02" / "ubuntu-controlled-trial.sh"


@pytest.mark.parametrize("extra", [
    ["--run-trial", "--class", "tcp"], ["--run-trial", "--class", "gui"],
    ["--run-trial", "--class", "c-child"], ["--run-trial", "--class", "c-single;id"],
    ["--run-trial", "--class", "C-SINGLE"], ["--reboot-prepare", "--class", "py"],
    ["--reboot-resume", "--class", "c-threads"], ["--run-trial", "--class"],
    ["--run-trial", "--reboot-prepare"],
])
def test_ubuntu_script_class_selection_is_strictly_allowlisted(extra: list[str]) -> None:
    done = subprocess.run(["bash", str(SCRIPT), "a" * 40, *extra], capture_output=True,
                          text=True, timeout=30)
    assert done.returncode == 2 and "usage:" in done.stderr


def test_ubuntu_script_maps_harness_refusal_to_blocked() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert '--class "$CLASS"' in text
    assert '[ $rc -ne 2 ] || blocked "harness refused $CLASS' in text


# ------------------------- restore_and_verify integration: file, continuity, exposure


class FakeRestoreBackend(backends.Dmtcp):
    def __init__(self) -> None:
        self.tools = None
        self.version = "dmtcp_launch (DMTCP) 4.2.0"
        self.controlled_trial = True
        self.listeners = []

    def restore(self, images: Sequence[Path],
                not_before_ticks: int) -> tuple[Check, Session | None]:
        return (Check("restore command started", Status.PASS, "fake"),
                Session(runner.Tracked("fake", runner.ProcId(1, 1, 0))))

    def restored_pid(self, kpid: int | None, pid: int) -> int | None:
        return None


def prepared_file_static(tmp_path: Path, restored_counters: list[int],
                         held_after: str) -> tuple[trial.TrialState, dict[str, object]]:
    run_dir = tmp_path / "run"
    (run_dir / "reports").mkdir(parents=True)
    original = [ngstate(c) for c in (0, 10, 20, 30, 40, 450)]
    restored = [ngstate(c) for c in restored_counters]
    (run_dir / "reports" / "managed.txt").write_text("\n".join(original + restored) + "\n",
                                                     encoding="utf-8")
    header = f"NGHOLD v1 nonce={NONCE}\n"
    (run_dir / "held.txt").write_text(held_after, encoding="utf-8")
    base = {"exists": True, "size": len(header),
            "sha256": hashlib.sha256(header.encode()).hexdigest()}
    state = trial.TrialState(run_dir, runner.Recorder(run_dir))
    state.baseline_listeners = set()
    state.facts.update(held_file_at_checkpoint=base, held_file_at_kill=base)
    marks: dict[str, object] = {"lower": {"main": 30}, "upper": {"main": 40},
                                "kill": {"main": 450}, "kill_line": len(original),
                                "nonce": NONCE, "original_dead": True}
    return state, marks


def run_restore(state: trial.TrialState, marks: dict[str, object],
                monkeypatch: pytest.MonkeyPatch, listeners: list[runner.Listener]) -> None:
    monkeypatch.setattr(trial, "boot_ticks_now", lambda: 0)
    monkeypatch.setattr(trial, "current_listeners", lambda: listeners)
    opts = trial.Options(backend="dmtcp", workload_class="c-file-static", run_root=state.run_dir,
                         dmtcp_controlled_trial=True, observe_s=0.0)
    arts = runner.ArtifactSet(str(state.run_dir), "ext4", "/dev/vda2")
    trial.restore_and_verify(state, opts, trial.CLASSES["c-file-static"], FakeRestoreBackend(),
                             arts, marks)


def statuses(state: trial.TrialState) -> dict[str, Status]:
    return {c.name: c.status for c in state.checks}


def test_file_static_restore_checks_file_and_continuity(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    state, marks = prepared_file_static(tmp_path, [40, 50, 60], f"NGHOLD v1 nonce={NONCE}\n")
    run_restore(state, marks, monkeypatch, [])
    got = statuses(state)
    assert got["held file consistency (c-file-static)"] is Status.PASS
    assert got["restored state continuity (main)"] is Status.PASS
    assert "restore_timing_definition" in state.facts


def test_file_static_restore_fails_when_the_file_changed(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    state, marks = prepared_file_static(tmp_path, [40, 50, 60],
                                        f"NGHOLD v1 nonce={NONCE}\nunexpected write\n")
    run_restore(state, marks, monkeypatch, [])
    assert statuses(state)["held file consistency (c-file-static)"] is Status.FAIL


def test_file_static_restore_aborts_on_network_exposure(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    state, marks = prepared_file_static(tmp_path, [40, 50, 60], f"NGHOLD v1 nonce={NONCE}\n")
    exposed = runner.parse_ss_listeners("LISTEN 0 128 0.0.0.0:41000 0.0.0.0:*\n")
    with pytest.raises(backends.BackendStepError, match="beyond loopback"):
        run_restore(state, marks, monkeypatch, exposed)
    assert not any(n.startswith("restored state continuity") for n in statuses(state))


def test_each_phase_keeps_its_own_result_file(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trial, "environment", lambda run_root: {})
    state = trial.TrialState(tmp_path, runner.Recorder(tmp_path))
    opts = trial.Options(backend="dmtcp", workload_class="c-single", run_root=tmp_path)
    state.add(Check("native checkpoint created", Status.PASS, "x", facts={"duration_s": 1.23}))
    trial.write_result(state, opts, "reboot-prepare")
    state.checks = [Check("restored state continuity (main)", Status.PASS, "y")]
    trial.write_result(state, opts, "reboot-resume")
    kinds = {json.loads(p.read_text())["kind"] for p in tmp_path.glob("result-*.json")}
    assert kinds == {"reboot-prepare", "reboot-resume"}
    rows = trial.summarize_runs(tmp_path.parent)
    assert {r["kind"] for r in rows if r["run"] == tmp_path.name} == kinds
    assert next(r for r in rows if r["kind"] == "reboot-prepare")["checkpoint_s"] == 1.23
