"""MVP Stages 07 and 08: scheduler timing, no overlapping operations, battery readings."""

from __future__ import annotations

from pathlib import Path

import pytest
from mvp_fakes import FakeBackend, fake_build

from napguard.daemon import scheduler
from napguard.daemon.manager import Manager
from napguard.daemon.ops import Executor
from napguard.power.battery import BatteryMonitor, read_power_supply
from napguard.storage.catalog import Catalog

T0 = "2026-10-21T10:00:00Z"


def test_due_respects_interval_and_minimum() -> None:
    start = scheduler.epoch(T0)
    eligible = [("a", T0), ("b", T0)]
    assert scheduler.due(start + 59, 300, eligible, {}) == []
    assert scheduler.due(start + 300, 300, eligible, {"b": "2026-10-21T10:04:00Z"}) == ["a"]
    assert scheduler.due(start + 60, 5, eligible, {}) == ["a", "b"]  # floor of 60 s (FR-013)


def test_scheduler_is_off_by_default_and_submits_when_enabled() -> None:
    submitted: list[str] = []
    settings = {"auto_checkpoint_enabled": False, "auto_checkpoint_interval_s": 60}
    def submit(workload_id: str) -> str:
        submitted.append(workload_id)
        return "op"

    s = scheduler.Scheduler(lambda: settings, lambda: [("a", T0)], dict, submit,
                            clock=lambda: scheduler.epoch(T0) + 61)
    assert s.tick() == [] and submitted == []
    settings["auto_checkpoint_enabled"] = True
    assert s.tick() == ["op"] and submitted == ["a"]


def test_executor_merges_and_never_overlaps(tmp_path: Path) -> None:
    data = tmp_path / "d"
    data.mkdir(mode=0o700)
    cat = Catalog(data / "c.db")
    m = Manager(cat, FakeBackend(), data, build=fake_build, verify_window_s=0.2,
                report_timeout_s=10)
    ex = Executor(cat, m)
    launch = ex.submit("launch", params={"workload_class": "c-threads"})
    ex.run_one(launch)
    wid = m.workloads()[0]["id"]
    first = ex.submit("checkpoint", workload_id=wid, trigger="periodic")
    assert ex.submit("checkpoint", workload_id=wid) == first  # merged (FR-014)
    ex.run_one(first)
    op = cat.operation(first)
    assert op is not None and op["state"] == "SUCCEEDED"
    bad = ex.submit("restore", checkpoint_id="ck-nope")
    ex.run_one(bad)
    failed = cat.operation(bad)
    assert failed is not None and failed["state"] == "FAILED" and failed["error_code"] == "NOT_FOUND"
    running = cat.query("SELECT COUNT(*) AS n FROM operations WHERE state='RUNNING'")
    assert running == [{"n": 0}]
    m.shutdown()


def supply(root: Path, name: str, **files: str) -> None:
    d = root / name
    d.mkdir(parents=True)
    for key, value in files.items():
        (d / key).write_text(value + "\n", encoding="utf-8")


def test_no_battery_is_reported_unavailable(tmp_path: Path) -> None:
    assert read_power_supply(tmp_path / "absent").available is False
    supply(tmp_path, "AC", type="Mains", online="1")
    reading = read_power_supply(tmp_path)
    assert not reading.available and reading.source == "NONE" and reading.ac_online is True


def test_real_batteries_are_averaged(tmp_path: Path) -> None:
    supply(tmp_path, "BAT0", type="Battery", present="1", capacity="40", status="Discharging")
    supply(tmp_path, "BAT1", type="Battery", present="1", capacity="60", status="Discharging")
    reading = read_power_supply(tmp_path)
    assert (reading.available, reading.source, reading.percent) == (True, "SYSFS", 50)
    assert reading.to_json()["simulated"] is False


def test_simulated_readings_are_labelled_and_triggers_fire_once(tmp_path: Path) -> None:
    mon = BatteryMonitor(tmp_path / "absent")
    assert mon.evaluate(20, 10) == (mon.current(), [])
    reading = mon.simulate(18, ac_online=False)
    assert reading.source == "SIMULATED" and reading.to_json()["simulated"] is True
    assert mon.evaluate(20, 10)[1] == ["low"]
    assert mon.evaluate(20, 10)[1] == []  # once per discharge episode
    mon.simulate(9, ac_online=False)
    assert mon.evaluate(20, 10)[1] == ["critical"]
    mon.simulate(9, ac_online=True)  # charging re-arms
    assert mon.evaluate(20, 10)[1] == []
    mon.simulate(15, ac_online=False)
    assert mon.evaluate(20, 10)[1] == ["low"]
    mon.clear_simulation()
    assert mon.current().source == "NONE"
    with pytest.raises(ValueError):
        mon.simulate(101, False)
