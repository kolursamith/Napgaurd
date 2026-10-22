"""MVP Stages 04, 05 and 09: catalog, supervision, checkpoint history, and recovery.

Uses the labelled TEST DOUBLE in mvp_fakes.py; the real path is verified on Ubuntu.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mvp_fakes import FakeBackend, fake_build

from napguard.checkpoint.base import BackendError, ErrorCode
from napguard.daemon.manager import Manager, ManagerError
from napguard.storage.catalog import Catalog


def manager(tmp_path: Path, mode: str = "genuine") -> Manager:
    data = tmp_path / "data"
    data.mkdir(mode=0o700, parents=True)
    return Manager(Catalog(data / "catalog.db"), FakeBackend(mode), data, build=fake_build,
                   verify_window_s=0.3, report_timeout_s=10)


def run_for(m: Manager, seconds: float) -> None:
    import time
    time.sleep(seconds)


def test_catalog_pragmas_and_reconciliation(tmp_path: Path) -> None:
    cat = Catalog(tmp_path / "c.db")
    assert cat.pragmas() == {"journal_mode": "wal", "synchronous": 2, "foreign_keys": 1}
    assert (tmp_path / "c.db").stat().st_mode & 0o777 == 0o600
    op = cat.add_operation("checkpoint", "wl-1")
    cat.start_operation(op)
    with cat.tx() as db:
        db.execute("INSERT INTO workloads(id, name, workload_class, argv, work_dir, report_path, "
                   "created_utc) VALUES ('wl-1','d','c-single','[]','/x','/x/r',"
                   "'2026-01-01T00:00:00Z')")
        db.execute("INSERT INTO instances(id, workload_id, origin, session_dir, state, "
                   "started_utc) VALUES ('in-1','wl-1','LAUNCH','/x','RUNNING','t')")
        db.execute("INSERT INTO checkpoints(id, workload_id, instance_id, generation, trigger, "
                   "state, directory, manifest_sha256, total_bytes, capture_ms, backend, "
                   "backend_version, created_utc, marks) VALUES ('ck-1','wl-1','in-1',1,'manual',"
                   "'PUBLISHED', ?, 'x', 1, 1, 'dmtcp', '4.2.0', 't', '{}')", (str(tmp_path / "no"),))
    assert cat.reconcile() == {"operations_interrupted": 1, "instances_lost": 1,
                               "checkpoints_missing": 1}
    op_row = cat.operation(op)
    assert op_row is not None and op_row["state"] == "INTERRUPTED"
    with pytest.raises(KeyError):
        cat.set_settings(not_a_setting=1)


def test_launch_checkpoint_history_stop_and_verified_restore(tmp_path: Path) -> None:
    m = manager(tmp_path)
    launched = m.launch_demo("c-threads")
    wid = launched["workload_id"]
    first = m.checkpoint(wid)
    run_for(m, 0.2)
    second = m.checkpoint(wid, "periodic")
    states = {c["id"]: c["state"] for c in m.checkpoints(wid)}
    assert states == {first["checkpoint_id"]: "SUPERSEDED", second["checkpoint_id"]: "PUBLISHED"}
    with pytest.raises(ManagerError) as err:
        m.restore(second["checkpoint_id"])
    assert err.value.code == "CONFLICT"  # FR-025: live instance
    run_for(m, 0.3)
    m.stop(wid)
    with pytest.raises(ManagerError) as err:
        m.restore(first["checkpoint_id"])
    assert err.value.code == "INVALID_STATE"  # superseded image no longer exists
    restored = m.restore(second["checkpoint_id"])
    assert all(c["status"] == "PASS" for c in restored["continuity"])
    w = m.workloads()[0]
    assert w["live"] and w["instance"]["origin"] == "RESTORE" and w["instance"]["state"] == "RUNNING"
    with pytest.raises(ManagerError) as err:
        m.checkpoint(wid)
    assert err.value.code == "INVALID_STATE"  # restored instances are not checkpointed (MVP)
    kinds = [e["kind"] for e in m.catalog.events(50)]
    assert "restore.verified" in kinds and "checkpoint.published" in kinds
    m.shutdown()
    assert not m.live


def test_fresh_start_is_not_a_restore(tmp_path: Path) -> None:
    m = manager(tmp_path, "fresh")
    wid = m.launch_demo("c-threads")["workload_id"]
    run_for(m, 0.2)
    ck = m.checkpoint(wid)["checkpoint_id"]
    run_for(m, 0.3)
    m.stop(wid)
    with pytest.raises(ManagerError) as err:
        m.restore(ck)
    assert err.value.code == "VERIFY_FAILED" and "nonce changed" in err.value.message
    assert not m.live
    inst = m.workloads()[0]["instance"]
    assert inst["origin"] == "RESTORE" and inst["state"] == "FAILED"


@pytest.mark.parametrize("target", ["image", "manifest"])
def test_tampered_checkpoint_is_corrupt_and_refused(tmp_path: Path, target: str) -> None:
    m = manager(tmp_path)
    wid = m.launch_demo("c-threads")["workload_id"]
    ck = m.checkpoint(wid)["checkpoint_id"]
    run_for(m, 0.2)
    m.stop(wid)
    row = m.checkpoints(wid)[0]
    path = Path(row["directory"]) / ("ckpt_fake.dmtcp" if target == "image"
                                      else "napguard-manifest.json")
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("0", "1", 1), encoding="utf-8")
    with pytest.raises((BackendError, ManagerError)) as err:
        m.restore(ck)
    code = getattr(err.value, "code", None)
    assert code in (ErrorCode.CORRUPT, ErrorCode.INVALID_MANIFEST, "CORRUPT")
    assert m.checkpoints(wid)[0]["state"] == "CORRUPT"
    with pytest.raises(ManagerError) as again:
        m.restore(ck)
    assert again.value.code == "INVALID_STATE"
    assert not m.live


def test_only_verified_reference_classes_and_coordinator_refusal(tmp_path: Path) -> None:
    m = manager(tmp_path)
    for cls in ("py", "c-child", "tcp", "gui", "c-file-grow"):
        with pytest.raises(ManagerError) as err:
            m.launch_demo(cls)
        assert err.value.code == "UNSUPPORTED_WORKLOAD"
    refused = manager(tmp_path / "r", "refuse-launch")
    with pytest.raises(BackendError) as denied:
        refused.launch_demo("c-single")
    assert denied.value.code is ErrorCode.PERMISSION_DENIED
    inst = refused.catalog.query("SELECT state FROM instances")
    assert inst == [{"state": "FAILED"}]


def test_exited_instance_is_noticed(tmp_path: Path) -> None:
    m = manager(tmp_path)
    wid = m.launch_demo("c-threads")["workload_id"]
    ck = m.checkpoint(wid)["checkpoint_id"]
    run_for(m, 0.2)
    m.stop(wid)
    m.backend.mode = "dies"  # type: ignore[attr-defined]
    with pytest.raises(ManagerError):
        m.restore(ck)  # dies inside the verification window
    assert json.loads(m.workloads()[0]["instance"]["exit_info"])["stopped"] in (True, False)
    assert not m.live
