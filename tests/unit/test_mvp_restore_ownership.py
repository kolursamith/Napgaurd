"""Restore ownership (Ubuntu GUI restore: "VERIFY_FAILED: the reporting process is not this
restore's process") and repeated checkpoints/restores. Labelled TEST DOUBLE backend.

Root cause: the reference workload reads its kernel PID, then composes and writes a
report; DMTCP may checkpoint it in between, so the restored process first writes a line
carrying the ORIGINAL's PID. That line is accepted only as the first line of its role
with the dead original's PID; everything else must come from this restore's own process.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from mvp_fakes import FakeBackend, fake_build

from napguard.daemon.manager import Manager, ManagerError
from napguard.selftest import model, verify
from napguard.storage.catalog import Catalog

NONCE = "cd" * 16


def obs(line: int, role: str, kpid: int | None, counter: int = 10) -> model.Observation:
    return model.Observation("r", role, 1, kpid, NONCE, counter, 0, None, 0, line)


def problems(lines: list[model.Observation], original: set[int] | None = None,
             mine: frozenset[int] = frozenset({200})) -> list[str]:
    return verify.ownership_problems(lines, original_kpids={100} if original is None else original,
                                     adopt=lambda k: k in mine)


def test_ownership_rule() -> None:
    assert problems([obs(1, "main", 200), obs(2, "main", 200)]) == []
    # the in-flight line: first line of the role, with the dead original's PID
    assert problems([obs(1, "main", 100), obs(2, "main", 200)]) == []
    assert problems([obs(1, "main", 100), obs(2, "t0", 100), obs(3, "main", 200),
                     obs(4, "t0", 200)]) == []
    for bad, why in [
        ([obs(1, "main", 200), obs(2, "main", 100)], "not this restore's process"),  # after owned
        ([obs(1, "main", 100), obs(2, "main", 100), obs(3, "main", 200)], "not this restore's"),
        ([obs(1, "main", 300), obs(2, "main", 200)], "300"),  # a foreign process
        ([obs(1, "main", None), obs(2, "main", 200)], "no kernel PID"),
        ([obs(1, "main", 100)], "no report from this restore's own process"),
    ]:
        found = problems(bad)
        assert found and why in found[0], (bad, found)
    # original not confirmed dead: its PID is never excused
    assert problems([obs(1, "main", 100), obs(2, "main", 200)], original=set())
    # PID reuse: if the original's PID is adoptable now, it is simply this restore's process
    assert problems([obs(1, "main", 100), obs(2, "main", 100)], mine=frozenset({100})) == []


def manager(tmp_path: Path, mode: str) -> Manager:
    data = tmp_path / "data"
    data.mkdir(mode=0o700, parents=True)
    return Manager(Catalog(data / "catalog.db"), FakeBackend(mode), data, build=fake_build,
                   verify_window_s=0.3, report_timeout_s=10)


@pytest.mark.parametrize("cls", ["c-threads"])
def test_inflight_report_restores_with_continuity(tmp_path: Path, cls: str) -> None:
    m = manager(tmp_path, "inflight")
    wid = m.launch_demo(cls)["workload_id"]
    m.checkpoint(wid)
    time.sleep(0.2)
    ck = m.checkpoint(wid)["checkpoint_id"]  # generation 2, as in the GUI report
    time.sleep(0.3)
    m.stop(wid)
    result = m.restore(ck)
    assert all(c["status"] == "PASS" for c in result["continuity"])
    m.shutdown()


def test_a_stray_writer_is_still_refused(tmp_path: Path) -> None:
    m = manager(tmp_path, "stray-writer")
    wid = m.launch_demo("c-threads")["workload_id"]
    ck = m.checkpoint(wid)["checkpoint_id"]
    time.sleep(0.3)
    m.stop(wid)
    with pytest.raises(ManagerError) as err:
        m.restore(ck)
    assert err.value.code == "VERIFY_FAILED" and "9999" in err.value.message
    assert err.value.detail["ownership"]
    assert not m.live
    m.shutdown()


def test_repeated_checkpoints_and_restores_after_a_successful_restore(tmp_path: Path) -> None:
    m = manager(tmp_path, "inflight")
    wid = m.launch_demo("c-threads")["workload_id"]
    for _ in range(3):
        last = m.checkpoint(wid)["checkpoint_id"]
        time.sleep(0.15)
    time.sleep(0.2)
    m.stop(wid)
    states = [c["state"] for c in m.checkpoints(wid)]
    assert states.count("PUBLISHED") == 1 and states.count("SUPERSEDED") == 2
    for attempt in range(3):  # restore, stop, restore the same checkpoint again ...
        result = m.restore(last)
        assert all(c["status"] == "PASS" for c in result["continuity"]), attempt
        time.sleep(0.2)
        m.stop(wid)
    instances = m.catalog.query("SELECT origin, state FROM instances ORDER BY started_utc, rowid")
    assert instances == [{"origin": "LAUNCH", "state": "STOPPED"}] + \
        [{"origin": "RESTORE", "state": "STOPPED"}] * 3
    m.shutdown()
