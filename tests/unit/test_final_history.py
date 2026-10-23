"""History management, migration, eligibility and the new daemon APIs.

Labelled TEST DOUBLE backend (mvp_fakes.py) with real files, real SQLite, real sockets.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from mvp_fakes import FakeBackend, fake_build

from napguard.daemon import history as history_mod
from napguard.daemon.history import HistoryError, safe_remove_tree
from napguard.daemon.manager import Manager, ManagerError
from napguard.daemon.ops import Executor
from napguard.storage.catalog import Catalog


def make_manager(tmp_path: Path) -> tuple[Manager, Executor]:
    data = tmp_path / "data"
    data.mkdir(mode=0o700, parents=True)
    m = Manager(Catalog(data / "catalog.db"), FakeBackend(), data, build=fake_build,
                verify_window_s=0.2, report_timeout_s=10)
    return m, Executor(m.catalog, m)


def finished(m: Manager, checkpoints: int = 1) -> tuple[str, list[str]]:
    wid = m.launch_demo("c-threads")["workload_id"]
    cids = []
    for _ in range(checkpoints):
        cids.append(m.checkpoint(wid)["checkpoint_id"])
        time.sleep(0.15)
    time.sleep(0.15)
    m.stop(wid)
    return wid, cids


def run(ex: Executor, kind: str, **kw: object) -> dict[str, object]:
    op = ex.submit(kind, **kw)  # type: ignore[arg-type]
    ex.run_one(op)
    row = ex.catalog.operation(op)
    assert row is not None
    return row


def test_v1_database_is_migrated_without_losing_data(tmp_path: Path) -> None:
    db = sqlite3.connect(tmp_path / "old.db")
    db.executescript("""
      CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
      INSERT INTO meta VALUES ('schema_version','1');
      CREATE TABLE workloads(id TEXT PRIMARY KEY, name TEXT NOT NULL, workload_class TEXT NOT NULL,
        argv TEXT NOT NULL, work_dir TEXT NOT NULL, report_path TEXT NOT NULL,
        created_utc TEXT NOT NULL);
      INSERT INTO workloads VALUES ('wl-000000000001','demo','c-single','[]','/w','/w/r','t');
    """)
    db.commit()
    db.close()
    cat = Catalog(tmp_path / "old.db")
    assert cat.migrations_applied == [2] and cat.schema_version() == 2
    row = cat.one("SELECT * FROM workloads")
    assert row is not None and row["name"] == "demo" and row["archived_utc"] is None
    cat.close()
    assert Catalog(tmp_path / "old.db").migrations_applied == []  # idempotent


def test_archive_hides_and_never_touches_live_workloads(tmp_path: Path) -> None:
    m, ex = make_manager(tmp_path)
    live = m.launch_demo("c-threads")["workload_id"]
    done, _ = finished(m)
    refused = run(ex, "archive", workload_id=live)
    assert refused["state"] == "FAILED" and refused["error_code"] == "CONFLICT"
    assert run(ex, "archive", workload_id=done)["state"] == "SUCCEEDED"
    assert [w["id"] for w in m.workloads()] == [live]
    assert {w["id"] for w in m.workloads(include_archived=True)} == {live, done}
    assert run(ex, "unarchive", workload_id=done)["state"] == "SUCCEEDED"
    assert len(m.workloads()) == 2
    m.shutdown()


def test_preview_then_delete_only_finished_history(tmp_path: Path) -> None:
    m, ex = make_manager(tmp_path)
    live = m.launch_demo("c-threads")["workload_id"]
    done, _ = finished(m, 2)
    outside = tmp_path / "precious.txt"
    outside.write_text("user data", encoding="utf-8")
    work = Path(m._workload(done)["work_dir"])
    (work / "link-to-user-file").symlink_to(outside)
    (work / "link-to-home").symlink_to(tmp_path, target_is_directory=True)
    preview = ex.history.preview()
    assert [e["workload_id"] for e in preview["eligible"]] == [done]
    assert preview["blocked"] == [{"workload_id": live,
                                   "reason": "it has a live instance: stop it first"}]
    entry = preview["eligible"][0]
    assert entry["directory"] == str(work) and entry["checkpoints"] == 2 and entry["files"] > 0
    result = run(ex, "delete_history", params={"workload_ids": [done, live]})
    assert result["state"] == "SUCCEEDED"
    assert result["result"]["deleted"] == [done]  # type: ignore[index]
    assert result["result"]["blocked"][0]["workload_id"] == live  # type: ignore[index]
    assert not work.exists() and outside.read_text(encoding="utf-8") == "user data"
    assert tmp_path.is_dir()  # the directory symlink was removed, not followed
    assert m.catalog.query("SELECT id FROM checkpoints WHERE workload_id=?", (done,)) == []
    assert any(e["kind"] == "history.deleted" for e in m.catalog.events(50))  # audit kept
    assert Path(m._workload(live)["work_dir"]).is_dir()
    m.shutdown()


def test_busy_workloads_and_bad_ids_are_refused(tmp_path: Path) -> None:
    m, ex = make_manager(tmp_path)
    done, cids = finished(m)
    m.catalog.add_operation("restore", None, cids[0])  # queued, not yet run
    assert "queued or running" in str(ex.history.blocked_reason(done))
    with pytest.raises(HistoryError):
        ex.history.archive(done)
    with pytest.raises(HistoryError) as err:
        ex.history._directory("../../etc")
    assert err.value.code == "INVALID_ARGUMENT"


def test_delete_checkpoint_never_removes_a_newer_generation(tmp_path: Path) -> None:
    m, ex = make_manager(tmp_path)
    wid, (old, new) = finished(m, 2)
    assert run(ex, "delete_checkpoint", checkpoint_id=old)["state"] == "SUCCEEDED"
    assert m.verify(new)["ok"] is True  # the newest image is intact and restorable
    assert m.checkpoints(wid)[0]["restore_eligible"] is True
    gone = run(ex, "delete_checkpoint", checkpoint_id=new)
    assert gone["result"]["files_removed"]  # type: ignore[index]
    assert m.checkpoints(wid) == []
    m.shutdown()


def test_partial_failure_leaves_nothing_restorable(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    m, ex = make_manager(tmp_path)
    wid, (cid,) = finished(m)

    def broken(path: Path) -> None:
        raise OSError("disk error")

    monkeypatch.setattr(history_mod, "safe_remove_tree", broken)
    result = ex.history.delete([wid])
    assert result["failed"] and not result["deleted"]
    assert m.checkpoints(wid)[0]["state"] == "MISSING"
    with pytest.raises(ManagerError):
        m.restore(cid)


def test_reset_demo_is_refused_while_live_and_complete_after(tmp_path: Path) -> None:
    m, ex = make_manager(tmp_path)
    live = m.launch_demo("c-threads")["workload_id"]
    finished(m)
    m.catalog.set_settings(auto_checkpoint_enabled=True)
    refused = run(ex, "reset_demo")
    assert refused["state"] == "FAILED" and refused["error_code"] == "CONFLICT"
    m.stop(live)
    assert run(ex, "reset_demo")["state"] == "SUCCEEDED"
    assert m.workloads(include_archived=True) == []
    assert m.catalog.settings()["auto_checkpoint_enabled"] is False
    assert any(e["kind"] == "demo.reset" for e in m.catalog.events(50))
    assert not any((m.data_dir / "workloads").iterdir())
    m.shutdown()


def test_eligibility_reasons_and_interrupted_restore(tmp_path: Path) -> None:
    m, ex = make_manager(tmp_path)
    wid = m.launch_demo("c-threads")["workload_id"]
    first = m.checkpoint(wid)["checkpoint_id"]
    time.sleep(0.15)
    second = m.checkpoint(wid)["checkpoint_id"]
    rows = {c["id"]: c for c in m.checkpoints(wid)}
    assert rows[first]["eligibility_reason"].startswith("superseded")
    assert "running" in rows[second]["eligibility_reason"]
    time.sleep(0.2)
    m.stop(wid)
    assert m.checkpoints(wid)[0]["restore_eligible"] is True
    # an interrupted restore (daemon died mid-operation) leaves the checkpoint usable
    op = m.catalog.add_operation("restore", None, second)
    m.catalog.start_operation(op)
    with m.catalog.tx() as db:
        db.execute("INSERT INTO instances(id, workload_id, origin, checkpoint_id, session_dir, "
                   "state, started_utc) VALUES ('in-x', ?, 'RESTORE', ?, '/nowhere', 'STARTING',"
                   " 't')", (wid, second))
    assert m.catalog.reconcile()["operations_interrupted"] == 1
    assert m.verify(second)["ok"] is True
    assert m.checkpoints(wid)[0]["restore_eligible"] is True
    restored = m.restore(second)
    assert restored["continuity_verified"] is True
    assert restored["original_nonce"] == restored["restored_nonce"]
    assert restored["first_restored_counter"] is not None
    m.shutdown()


def test_safe_remove_tree_refuses_a_symlinked_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    with pytest.raises(HistoryError):
        safe_remove_tree(tmp_path / "link")
    assert real.is_dir()
