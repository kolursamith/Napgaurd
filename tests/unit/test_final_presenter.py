"""GUI view-model (pure, no tkinter): states, enabled actions, labels, guided demo."""

from __future__ import annotations

from typing import Any

import pytest

from napguard.ui import presenter as pr


def wl(state: str | None = "RUNNING", origin: str = "LAUNCH", live: bool = True,
       pending: dict[str, str] | None = None, archived: bool = False) -> dict[str, object]:
    return {"id": "wl-1", "live": live, "archived": archived, "pending_operation": pending,
            "instance": None if state is None else {"state": state, "origin": origin}}


@pytest.mark.parametrize(("w", "label"), [
    (wl(), "RUNNING"),
    (wl(origin="RESTORE"), "RESTORED"),
    (wl("STOPPED", live=False), "STOPPED"),
    (wl("FAILED", "RESTORE", live=False), "FAILED"),
    (wl("LOST", live=False), "LOST"),
    (wl(pending={"kind": "checkpoint", "state": "RUNNING"}), "CHECKPOINTING"),
    (wl("STOPPED", live=False, pending={"kind": "restore", "state": "RUNNING"}), "RESTORING"),
    (wl(pending={"kind": "checkpoint", "state": "QUEUED"}), "RUNNING"),  # queued, not started
    (wl(None, live=False), "NEVER STARTED"),
])
def test_display_state_is_derived_from_backend_records(w: dict[str, object], label: str) -> None:
    assert pr.display_state(w) == label
    assert label in pr.STATE_INFO


def test_actions_are_enabled_only_when_valid() -> None:
    running = pr.workload_actions(wl())
    assert running["checkpoint"].enabled and running["stop"].enabled
    assert not running["archive"].enabled and not running["delete"].enabled
    restored = pr.workload_actions(wl(origin="RESTORE"))
    assert not restored["checkpoint"].enabled and "ADR 0008" in restored["checkpoint"].reason
    stopped = pr.workload_actions(wl("STOPPED", live=False))
    assert not stopped["checkpoint"].enabled and not stopped["stop"].enabled
    assert stopped["archive"].enabled and stopped["delete"].enabled
    busy = pr.workload_actions(wl("STOPPED", live=False, pending={"kind": "restore",
                                                                 "state": "QUEUED"}))
    assert not any(a.enabled for k, a in busy.items() if k != "unarchive")
    assert pr.workload_actions(wl("STOPPED", live=False, archived=True))["unarchive"].enabled
    assert not any(a.enabled for a in pr.workload_actions(None).values())


def test_checkpoint_labels_and_history() -> None:
    assert pr.checkpoint_label({"state": "CORRUPT"}) == "INVALID"
    assert pr.integrity_text({"state": "CORRUPT"}) == "FAILED"
    assert pr.integrity_text({"state": "PUBLISHED", "verified_utc": None}) == "not verified"
    assert pr.restore_history_text({"restores": []}) == "never restored"
    assert pr.restore_history_text({"restores": [{"state": "STOPPED"}, {"state": "FAILED"}]}) \
        == "2 restore(s), 1 verified"


def test_activity_severity_search_and_simulated_label() -> None:
    failed = {"kind": "restore.failed", "message": "x", "data": {"checkpoint_id": "ck-1"}}
    sim = {"kind": "battery.low", "message": "15%", "simulated": True, "data": {}}
    ok = {"kind": "checkpoint.published", "message": "ck-2", "data": {"workload_id": "wl-9"}}
    assert [pr.severity(e) for e in (failed, sim, ok)] == ["Error", "Warning", "Success"]
    assert pr.event_text(sim).startswith("[SIMULATED]")
    assert pr.event_text(ok).startswith("Checkpoint created successfully.")
    assert pr.event_workload(ok) == "wl-9"
    assert pr.matches(ok, "All", "wl-9") and not pr.matches(ok, "Error", "")
    assert pr.matches(failed, "Error", "ck-1")


def test_every_error_code_has_a_suggested_action() -> None:
    for code in ("DAEMON_UNAVAILABLE", "PERMISSION_DENIED", "CONFLICT", "VERIFY_FAILED",
                 "RESTORE_FAILED", "CORRUPT", "INVALID_MANIFEST", "UNSUPPORTED_WORKLOAD"):
        assert pr.suggested_action(code) != pr.suggested_action("SOMETHING_ELSE")
    assert pr.suggested_action(None)


def test_dashboard_never_shows_unavailable_as_zero() -> None:
    cards = pr.card_values(None, connected=False)
    assert cards[0][1] == "Not running" and all(v == pr.NA for _, v, _ in cards[1:])
    status: dict[str, Any] = {"coordinator_mode": "developer (acknowledged)", "live_instances": 0,
              "checkpoints": 0, "settings": {"auto_checkpoint_enabled": False},
              "backend": {"available": True, "version": "4.2.0", "verified": True},
              "battery": {"available": False, "source": "NONE"}}
    values = {t: v for t, v, _ in pr.card_values(status, connected=True)}
    assert values["Active processes"] == "0" and values["Battery"] == "Unavailable"
    assert "verified" in values["DMTCP backend"]
    assert "does not expose battery hardware" in pr.battery_long(status["battery"])
    assert pr.battery_long({"available": True, "simulated": True, "percent": 9,
                            "ac_online": False}).startswith("SIMULATED")


def test_guided_demo_is_strictly_sequential() -> None:
    demo = pr.GuidedDemo()
    assert demo.next_index() == 0 and not demo.can_run(1)
    with pytest.raises(ValueError):
        demo.start(2)
    demo.start(0)
    assert demo.next_index() is None  # running
    demo.finish(0, True, "launched")
    assert demo.next_index() == 1
    demo.start(1)
    demo.finish(1, False, "not advancing")
    assert [s.status for s in demo.steps[2:]] == ["Blocked"] * 5
    assert demo.next_index() is None and not demo.done()  # never proceeds past a failure


def test_recovery_rows_show_not_available_never_invented_values() -> None:
    rows = dict(pr.recovery_rows(None))
    assert set(rows.values()) == {pr.NA}
    rows = dict(pr.recovery_rows({"original_pid": 10, "restored_pid": 20, "original_nonce": "a",
                                  "restored_nonce": "a", "counter_at_checkpoint": 40,
                                  "counter_before_termination": 400,
                                  "first_restored_counter": 50, "last_restored_counter": 90,
                                  "continuity_verified": True}))
    assert rows["Continuity verification"] == "PASSED" and rows["Restored PID"] == "20"
    assert dict(pr.recovery_rows({"continuity_verified": False}))[
        "Continuity verification"] == "FAILED"


def test_formatting() -> None:
    assert pr.human_bytes(None) == pr.NA and pr.human_bytes(512) == "512 B"
    assert pr.age(None) == pr.NA
    import calendar
    import time
    base = calendar.timegm(time.strptime("2026-10-21T10:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
    assert pr.age("2026-10-21T10:00:00Z", now=base + 90) == "1 min ago"
    assert pr.age("2026-10-21T10:00:00Z", now=base + 5) == "5 s ago"
    assert pr.short_time("2026-10-21T10:00:00Z") == "2026-10-21 10:00:00 UTC"
