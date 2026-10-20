"""Stage 02 cross-reboot gates (ADR 0004 amendment), tested with a fake DMTCP backend.

No process is started and nothing is restored: these tests prove that every
precondition stops the resume BEFORE a restore is attempted, and that the prepare
step never writes a manifest unless every coordinator and process is gone.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from ngexp import backends, runner, trial, verify
from ngexp.backends import Session
from ngexp.results import Check, Status

HOST = {"arch": "aarch64", "machine_id_sha256": "m" * 64}
VERSION = "dmtcp_launch (DMTCP) 4.2.0"


class FakeDmtcp(backends.Dmtcp):
    """A Dmtcp that never runs anything; records what the resume asked it to do."""

    def __init__(self, isolation_ok: bool = True, restore_ok: bool = False) -> None:
        self.tools = None
        self.version = VERSION
        self.controlled_trial = True
        self.listeners = []
        self.isolation_ok = isolation_ok
        self.restore_ok = restore_ok
        self.isolation_calls = 0
        self.restore_calls = 0

    def probe(self) -> Check:
        return Check("backend available", Status.PASS, VERSION)

    def isolation_check(self) -> list[Check]:
        self.isolation_calls += 1
        status = Status.PASS if self.isolation_ok else Status.FAIL
        return [Check("coordinator confined to loopback", status, "fake")]

    def restore(self, images: Sequence[Path], not_before_ticks: int) -> tuple[Check, Session | None]:
        self.restore_calls += 1
        if not self.restore_ok:
            return Check("restore command started", Status.FAIL, "fake restore failure"), None
        return (Check("restore command started", Status.PASS, "fake"),
                Session(runner.Tracked("fake-restored", runner.ProcId(1, 1, os.getuid()))))

    def cleanup(self, sessions: Sequence[Session]) -> None:
        return None


@pytest.fixture
def prepared(tmp_path: Path) -> Path:
    run_dir = tmp_path / "runs" / "20261020T000000Z-reboot-dmtcp-c-single-abcd"
    (run_dir / "checkpoint").mkdir(parents=True)
    (run_dir / "bin").mkdir()
    image = run_dir / "checkpoint" / "ckpt_ngcounter_x.dmtcp"
    image.write_bytes(b"\x1f\x8b" + b"image" * 100)
    (run_dir / "bin" / "ngcounter").write_bytes(b"\x7fELF fake")
    manifest = {
        "boot_id": "old-boot", "host": HOST, "adr_0004_controlled_trial": True,
        "backend_version": VERSION, "tool_sha256": {}, "nonce": "a" * 32,
        "options": {"backend": "dmtcp", "workload_class": "c-single",
                    "run_root": str(tmp_path / "runs"), "dmtcp_prefix": None, "criu_bin": None,
                    "criu_mode": "unprivileged", "run_on_s": 4.0, "observe_s": 3.0},
        "marks": {"lower": {"main": 30}, "upper": {"main": 40}, "kill": {"main": 450},
                  "kill_line": 10, "nonce": "a" * 32, "original_dead": True},
        "artifacts": [{"path": "checkpoint/ckpt_ngcounter_x.dmtcp", "size": image.stat().st_size,
                       "sha256": runner.sha256_file(image), "mode": "600",
                       "is_symlink": False, "target": None}],
        "artifact_dir": str(run_dir / "checkpoint"),
        "sessions": [],
        "workload_binary_sha256": runner.sha256_file(run_dir / "bin" / "ngcounter"),
    }
    (run_dir / trial.REBOOT_MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    return run_dir


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch host facts; tests change 'boot' / 'listeners' / 'leaks' as needed."""
    facts: dict[str, Any] = {"boot": "new-boot", "listeners": [[]], "leaks": []}
    monkeypatch.setattr(trial, "boot_id", lambda: facts["boot"])
    monkeypatch.setattr(trial, "host_identity", lambda: HOST)
    monkeypatch.setattr(trial, "environment", lambda run_root: {})
    monkeypatch.setattr(trial, "run_dir_processes", lambda d: facts["leaks"])
    # Linux-only host facts read on the way to the restore step.
    monkeypatch.setattr(trial, "collect_artifacts",
                        lambda d, run_dir, patterns: runner.ArtifactSet(str(d), "ext4", "/dev/vda2"))
    monkeypatch.setattr(trial, "boot_ticks_now", lambda: 0)

    def listeners() -> list[runner.Listener]:
        seq: list[list[runner.Listener]] = facts["listeners"]
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(trial, "current_listeners", listeners)
    return facts


def resume(run_dir: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeDmtcp,
           **kwargs: Any) -> tuple[Status, dict[str, Check]]:
    def make(opts: trial.Options, rec: runner.Recorder) -> FakeDmtcp:
        fake.rec = rec
        return fake

    monkeypatch.setattr(trial, "make_backend", make)
    params: dict[str, Any] = {"backend_name": "dmtcp", "workload_class": "c-single",
                              "controlled": True}
    params.update(kwargs)
    status = trial.reboot_resume(run_dir, run_dir.parent, **params)
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    checks = {c["name"]: Check(c["name"], Status(c["status"]), c["detail"])
              for c in result["checks"]}
    return status, checks


def test_resume_reaches_restore_only_after_every_gate(prepared: Path, host: dict[str, Any],
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDmtcp(restore_ok=False)  # failed restoration
    status, checks = resume(prepared, monkeypatch, fake)
    assert fake.isolation_calls == 1 and fake.restore_calls == 1
    for name in ("system rebooted since checkpoint", "checkpoint artifacts intact after reboot",
                 "same backend build as before reboot", "workload binary unchanged",
                 "post-reboot isolation: coordinator confined to loopback"):
        assert checks[name].status is Status.PASS, name
    assert checks["restore command started"].status is Status.FAIL
    assert not any(n.startswith("restored state continuity") for n in checks)  # never claimed
    assert status is Status.FAIL and not (prepared / trial.RESUME_PASSED_MARK).exists()


def test_unchanged_boot_id_blocks_before_any_restore(prepared: Path, host: dict[str, Any],
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    host["boot"] = "old-boot"
    fake = FakeDmtcp()
    status, checks = resume(prepared, monkeypatch, fake)
    assert status is Status.BLOCKED
    assert checks["system rebooted since checkpoint"].status is Status.BLOCKED
    assert fake.isolation_calls == 0 and fake.restore_calls == 0


def test_missing_checkpoint_fails_before_restore(prepared: Path, host: dict[str, Any],
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    (prepared / "checkpoint" / "ckpt_ngcounter_x.dmtcp").unlink()
    fake = FakeDmtcp()
    status, checks = resume(prepared, monkeypatch, fake)
    assert status is Status.FAIL and "missing" in checks[
        "checkpoint artifacts intact after reboot"].detail
    assert fake.restore_calls == 0


def test_corrupt_checkpoint_fails_before_restore(prepared: Path, host: dict[str, Any],
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    image = prepared / "checkpoint" / "ckpt_ngcounter_x.dmtcp"
    data = bytearray(image.read_bytes())
    data[10] ^= 0xFF
    image.write_bytes(bytes(data))
    fake = FakeDmtcp()
    status, checks = resume(prepared, monkeypatch, fake)
    assert status is Status.FAIL and "modified" in checks[
        "checkpoint artifacts intact after reboot"].detail
    assert fake.restore_calls == 0


def test_wrong_dmtcp_version_fails_before_restore(prepared: Path, host: dict[str, Any],
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDmtcp()
    fake.version = "dmtcp_launch (DMTCP) 4.3.0"
    status, checks = resume(prepared, monkeypatch, fake)
    assert status is Status.FAIL
    assert checks["same backend build as before reboot"].status is Status.FAIL
    assert fake.isolation_calls == 0 and fake.restore_calls == 0


def test_failed_fresh_isolation_prevents_restore(prepared: Path, host: dict[str, Any],
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDmtcp(isolation_ok=False)
    status, checks = resume(prepared, monkeypatch, fake)
    assert status is Status.FAIL and fake.restore_calls == 0
    assert checks["post-reboot restore"].status is Status.SKIP


def test_listener_exposure_during_restore_aborts(prepared: Path, host: dict[str, Any],
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    wildcard = runner.parse_ss_listeners("LISTEN 0 128 0.0.0.0:41000 0.0.0.0:*\n")
    host["listeners"] = [[], wildcard]  # baseline empty; a new wildcard appears during restore
    fake = FakeDmtcp(restore_ok=True)
    status, checks = resume(prepared, monkeypatch, fake)
    assert status is Status.FAIL
    assert "beyond loopback" in checks["backend step"].detail
    assert not any(n.startswith("restored state continuity") for n in checks)


def test_cleanup_failure_fails_the_resume(prepared: Path, host: dict[str, Any],
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    host["leaks"] = [runner.ProcInfo(runner.ProcId(4242, 1, os.getuid()), "S", 4242, 4242)]
    status, checks = resume(prepared, monkeypatch, FakeDmtcp(restore_ok=False))
    assert status is Status.FAIL
    assert "untracked pid 4242" in checks["experiment processes cleaned up"].detail


@pytest.mark.parametrize(
    ("kwargs", "check_name"),
    [({"workload_class": "c-threads"}, "ADR 0004 exception scope"),
     ({"controlled": False}, "ADR 0004 flag matches the prepared run")],
)
def test_scope_or_flag_mismatch_blocks(prepared: Path, host: dict[str, Any],
                                       monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any],
                                       check_name: str) -> None:
    fake = FakeDmtcp()
    if kwargs.get("workload_class"):
        # The CLI class is checked against the manifest; the ADR scope check uses the manifest.
        status, checks = resume(prepared, monkeypatch, fake, **kwargs)
        assert checks["reboot-resume matches the prepared run"].status is Status.BLOCKED
    else:
        status, checks = resume(prepared, monkeypatch, fake, **kwargs)
        assert checks[check_name].status is Status.BLOCKED
    assert status is Status.BLOCKED and fake.restore_calls == 0


def test_second_resume_after_success_is_refused(prepared: Path, host: dict[str, Any],
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    (prepared / trial.RESUME_PASSED_MARK).write_text("done\n", encoding="utf-8")
    fake = FakeDmtcp()
    status, checks = resume(prepared, monkeypatch, fake)
    assert status is Status.BLOCKED and checks["not already resumed"].status is Status.BLOCKED
    assert fake.restore_calls == 0
    assert trial.latest_reboot_run(prepared.parent) is None  # not offered again


def test_missing_manifest_blocks(tmp_path: Path) -> None:
    assert trial.reboot_resume(tmp_path, tmp_path) is Status.BLOCKED


def test_process_identity_mismatch_stops_checkpoint(monkeypatch: pytest.MonkeyPatch,
                                                    tmp_path: Path) -> None:
    state = trial.TrialState(tmp_path, runner.Recorder(tmp_path))
    session = Session(runner.Tracked("wl", runner.ProcId(500, 1, os.getuid())))
    other = runner.ProcInfo(runner.ProcId(501, 1, os.getuid()), "S", 500, 500)
    me = runner.ProcInfo(runner.ProcId(500, 1, os.getuid()), "S", 500, 500)
    monkeypatch.setattr(trial, "running_group_members", lambda pgid: [me, other])
    monkeypatch.setattr(trial, "workload_socket_fds", lambda pid: [])
    with pytest.raises(backends.BackendStepError, match="single process"):
        trial.check_controlled_workload(state, session)
    monkeypatch.setattr(trial, "running_group_members", lambda pgid: [me])
    monkeypatch.setattr(trial, "workload_socket_fds", lambda pid: [(5, "socket:[1]")])
    with pytest.raises(backends.BackendStepError, match="no workload sockets"):
        trial.check_controlled_workload(state, session)


# ------------------------------------------------------------- reboot-prepare manifest gate


def fake_prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, cleanup_ok: bool,
                 coordinator_alive: bool) -> tuple[Status, Path]:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "bin").mkdir()
    (run_dir / "bin" / "ngcounter").write_bytes(b"bin")
    state = trial.TrialState(run_dir, runner.Recorder(run_dir))
    state.rec.tracked.append(runner.Tracked("coordinator", runner.ProcId(1, 1, os.getuid())))
    fake = FakeDmtcp()
    fake.rec = state.rec
    opts = trial.Options(backend="dmtcp", workload_class="c-single", run_root=tmp_path,
                         dmtcp_controlled_trial=True)
    arts = runner.ArtifactSet(str(run_dir / "checkpoint"), "ext4", "/dev/vda2")
    marks = {"nonce": "a" * 32, "lower": {}, "upper": {}, "kill": {}, "kill_line": 0,
             "original_dead": True}
    monkeypatch.setattr(trial, "prelude", lambda o, label, entry: (state, trial.CLASSES["c-single"],
                                                                     fake))
    monkeypatch.setattr(trial, "fresh_control", lambda s, wc: [])
    monkeypatch.setattr(trial, "launch_and_checkpoint", lambda s, o, wc, b: (arts, marks))
    monkeypatch.setattr(verify, "verify_fresh_control",
                        lambda fresh, nonce: Check("fresh-start control", Status.PASS, "fake"))
    monkeypatch.setattr(trial, "finish", lambda s, b: s.add(Check(
        "experiment processes cleaned up", Status.PASS if cleanup_ok else Status.FAIL, "fake")))
    monkeypatch.setattr(trial, "alive", lambda ident: coordinator_alive)
    monkeypatch.setattr(trial, "boot_id", lambda: "boot-1")
    monkeypatch.setattr(trial, "host_identity", lambda: HOST)
    monkeypatch.setattr(trial, "environment", lambda run_root: {})
    return trial.reboot_prepare(opts), run_dir


def test_prepare_writes_manifest_only_after_clean_shutdown(monkeypatch: pytest.MonkeyPatch,
                                                           tmp_path: Path) -> None:
    status, run_dir = fake_prepare(monkeypatch, tmp_path, cleanup_ok=True, coordinator_alive=False)
    assert status is Status.PASS
    manifest = json.loads((run_dir / trial.REBOOT_MANIFEST).read_text(encoding="utf-8"))
    assert manifest["boot_id"] == "boot-1" and manifest["nonce"] == "a" * 32
    assert manifest["adr_0004_controlled_trial"] is True and manifest["host"] == HOST


@pytest.mark.parametrize(("cleanup_ok", "coordinator_alive"), [(False, False), (True, True)])
def test_prepare_refuses_manifest_if_anything_survives(monkeypatch: pytest.MonkeyPatch,
                                                       tmp_path: Path, cleanup_ok: bool,
                                                       coordinator_alive: bool) -> None:
    status, run_dir = fake_prepare(monkeypatch, tmp_path, cleanup_ok=cleanup_ok,
                                   coordinator_alive=coordinator_alive)
    assert status is Status.FAIL
    assert not (run_dir / trial.REBOOT_MANIFEST).exists()  # nothing to resume: do not reboot
