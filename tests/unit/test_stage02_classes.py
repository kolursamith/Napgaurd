"""Preparation for further Stage 02 workload classes (no checkpoint is run here).

ADR 0004 authorises only `c-single`. These tests prove the harness is ready for the
proposed Amendment 2 classes (c-threads, py, c-file-static, c-file-grow) and still
refuses them today, and that evidence is summarised without inventing values.
"""

from __future__ import annotations

import json
import platform
import shutil
from pathlib import Path

import pytest

from ngexp import backends, cli, model, runner, trial, verify
from ngexp.results import Status

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


@pytest.mark.parametrize("cls", AMENDMENT_2)
def test_amendment_2_classes_are_still_refused(tmp_path: Path, cls: str) -> None:
    assert cls not in backends.CONTROLLED_TRIAL_CLASSES
    opts = trial.Options(backend="dmtcp", workload_class=cls, run_root=tmp_path,
                         dmtcp_controlled_trial=True)
    for entry in ("trial", "reboot-prepare", "reboot-resume"):
        refusal = trial.controlled_trial_refusal(opts, entry)
        assert refusal is not None and "not allowed under ADR 0004" in refusal


def test_only_c_single_is_authorised() -> None:
    assert backends.CONTROLLED_TRIAL_CLASSES == ("c-single",)
    assert backends.DMTCP_LOOPBACK_ONLY_COORDINATOR_VERIFIED is False


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
