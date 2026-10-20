"""Regression tests for the Stage 02 Ubuntu checkpoint failure (commit a088cd9).

`dmtcp_command --bcheckpoint` returned before the checkpoint was written (DMTCP 4.2.0 does
not honour the blocking prefix), and the harness rejected the checkpoint ~35 ms after it
started. Completion must be decided by protocol and artifact evidence, within a bound.
"""

from __future__ import annotations

import os
import platform
import signal
from collections.abc import Callable
from pathlib import Path

import pytest

from ngexp import backends, ckptwait, runner
from ngexp.backends import Session
from ngexp.results import Status

PORT = 45678
IMAGE = "ckpt_ngcounter_40000-1-1234.dmtcp"
SCRIPT = "dmtcp_restart_script_abc-40000-1234.sh"
linux_only = pytest.mark.skipif(platform.system() != "Linux", reason="needs Linux /proc")


def write_script(d: Path, port: int = PORT, image: str = IMAGE) -> None:
    (d / SCRIPT).write_text(f"coord_port={port}\nckpt_files='{d}/{image}'\n", encoding="utf-8")


class VirtualTime:
    """Deterministic clock: `sleep` advances time and runs scheduled filesystem events."""

    def __init__(self) -> None:
        self.now = 0.0
        self.events: list[tuple[float, Callable[[], None]]] = []

    def at(self, when: float, action: Callable[[], None]) -> None:
        self.events.append((when, action))

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        for when, action in sorted(self.events, key=lambda e: e[0]):
            if when <= self.now:
                action()
        self.events = [(w, a) for w, a in self.events if w > self.now]


def wait(d: Path, vt: VirtualTime, **kwargs: object) -> ckptwait.WaitResult:
    params: dict[str, object] = {"timeout": 30.0, "clock": vt.clock, "sleep": vt.sleep}
    params.update(kwargs)
    return ckptwait.wait_for_checkpoint(d, PORT, 0, 1, **params)  # type: ignore[arg-type]


def test_delayed_checkpoint_is_not_prematurely_rejected(tmp_path: Path) -> None:
    vt = VirtualTime()
    temp = tmp_path / f"{IMAGE}.temp"
    vt.at(1.5, grow_image(temp, 1000))

    def rename() -> None:
        temp.rename(tmp_path / IMAGE)

    def script() -> None:
        write_script(tmp_path)

    vt.at(2.5, rename)
    vt.at(2.75, script)
    result = wait(tmp_path, vt)
    assert result.ok, result.reason
    assert result.waited_s >= 3.25  # complete at 2.75 and then stable for >= 0.5 s
    assert result.evidence.images[0].name == IMAGE and result.evidence.scripts == [SCRIPT]


def grow_image(path: Path, size: int) -> Callable[[], None]:
    def action() -> None:
        path.write_bytes(b"x" * size)
    return action


def test_image_must_stop_changing_before_completion(tmp_path: Path) -> None:
    vt = VirtualTime()
    write_script(tmp_path)
    for i in range(1, 9):  # the image keeps growing until t = 2.0
        vt.at(i * 0.25, grow_image(tmp_path / IMAGE, 100 * i))
    result = wait(tmp_path, vt)
    assert result.ok and result.waited_s >= 2.5


def test_missing_image_times_out_with_diagnostics(tmp_path: Path) -> None:
    result = wait(tmp_path, VirtualTime(), timeout=5.0)
    assert not result.ok
    assert "timed out" in result.reason and "no ckpt_*.dmtcp image" in result.reason


def test_leftover_temp_image_never_counts_as_complete(tmp_path: Path) -> None:
    (tmp_path / IMAGE).write_bytes(b"old")
    (tmp_path / f"{IMAGE}.temp").write_bytes(b"partial")
    write_script(tmp_path)
    result = wait(tmp_path, VirtualTime(), timeout=3.0)
    assert not result.ok and "temporary checkpoint file" in result.reason


def test_image_without_restart_script_is_incomplete(tmp_path: Path) -> None:
    (tmp_path / IMAGE).write_bytes(b"x" * 10)
    result = wait(tmp_path, VirtualTime(), timeout=3.0)
    assert not result.ok and "restart script" in result.reason


def foreign_port(d: Path) -> None:
    write_script(d, port=11111)


def foreign_image(d: Path) -> None:
    write_script(d, image="ckpt_other.dmtcp")


def second_image(d: Path) -> None:
    (d / "ckpt_second.dmtcp").write_bytes(b"y")
    write_script(d)


def empty_image(d: Path) -> None:
    (d / IMAGE).write_bytes(b"")
    write_script(d)


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (foreign_port, "coordinator port"),
        (foreign_image, "does not reference image"),
        (second_image, "expected 1 image(s), found 2"),
        (empty_image, "size 0"),
    ],
)
def test_foreign_or_inconsistent_checkpoints_fail(tmp_path: Path, setup: Callable[[Path], None],
                                                  reason: str) -> None:
    (tmp_path / IMAGE).write_bytes(b"x" * 10)
    setup(tmp_path)
    result = wait(tmp_path, VirtualTime(), timeout=3.0)
    assert not result.ok and reason in result.reason


def test_files_older_than_the_request_are_ignored(tmp_path: Path) -> None:
    (tmp_path / IMAGE).write_bytes(b"x" * 10)
    write_script(tmp_path)
    future = (1 << 62)  # every existing file is "before" this request time
    ev = ckptwait.checkpoint_evidence(tmp_path, PORT, future, 1)
    assert not ev.complete and set(ev.stale) == {IMAGE, SCRIPT}


def test_workload_death_aborts_the_wait_immediately(tmp_path: Path) -> None:
    result = wait(tmp_path, VirtualTime(), liveness=lambda: "workload died")
    assert not result.ok and result.reason == "workload died" and result.polls == 1


def test_listener_audit_failure_propagates_during_the_wait(tmp_path: Path) -> None:
    def audit() -> None:
        raise backends.BackendStepError("experiment process listening beyond loopback")

    with pytest.raises(backends.BackendStepError, match="beyond loopback"):
        wait(tmp_path, VirtualTime(), audit=audit)


def test_parse_dmtcp_status() -> None:
    text = "Computation Id: abc\nCheckpoint Dir: /x\nNUM_PEERS=1\nRUNNING=yes\n"
    assert ckptwait.parse_dmtcp_status(text) == {"NUM_PEERS": "1", "RUNNING": "yes"}


def test_harness_no_longer_relies_on_bcheckpoint() -> None:
    source = (Path(backends.__file__)).read_text(encoding="utf-8")
    code_lines = [ln for ln in source.splitlines() if not ln.lstrip().startswith(("#", '"', "`"))]
    assert not any('"--bcheckpoint"' in ln for ln in code_lines)


# ----------------------------------------------- Linux: Dmtcp.checkpoint end to end (fake)

FAKE_COMMAND = f"""#!/bin/sh
# Fake dmtcp_command: '--checkpoint' returns at once (like DMTCP 4.2.0) and the
# "checkpoint" lands later in the background; '--status' reports one running peer.
for last; do :; done
case "$last" in
  --checkpoint)
    if [ "$FAKE_MODE" = delayed ]; then
      ( sleep 1.5; printf 'x%.0s' $(seq 1 2000) > "$CKPT/{IMAGE}.temp"
        mv "$CKPT/{IMAGE}.temp" "$CKPT/{IMAGE}"
        printf 'coord_port=%s\\n%s/{IMAGE}\\n' "$PORT" "$CKPT" > "$CKPT/{SCRIPT}" ) &
    fi ;;
  --status) printf 'NUM_PEERS=1\\nRUNNING=yes\\n' ;;
esac
exit 0
"""


def fake_checkpoint_setup(tmp_path: Path, mode: str,
                          monkeypatch: pytest.MonkeyPatch) -> tuple[backends.Dmtcp, Session]:
    rec = runner.Recorder(tmp_path / "run")
    dmtcp = backends.Dmtcp(rec, None, controlled_trial=True)
    dmtcp.ckpt_dir.mkdir(mode=0o700)
    tool = tmp_path / "fake_dmtcp_command"
    tool.write_text(FAKE_COMMAND, encoding="utf-8")
    tool.chmod(0o700)
    dmtcp.tools = backends.DmtcpTools(str(tool), str(tool), str(tool), str(tool))
    for key, value in {"FAKE_MODE": mode, "CKPT": str(dmtcp.ckpt_dir), "PORT": str(PORT)}.items():
        monkeypatch.setenv(key, value)  # restored after the test
    workload = rec.spawn("fake-workload", ["sleep", "30"], capture=False)
    coordinator = rec.spawn("fake-coordinator", ["sleep", "30"])
    return dmtcp, Session(workload, [coordinator], {"port": PORT})


@linux_only
def test_dmtcp_checkpoint_waits_for_late_completion(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    dmtcp, session = fake_checkpoint_setup(tmp_path, "delayed", monkeypatch)
    audits: list[str] = []
    try:
        check, _ = dmtcp.checkpoint(session, audit=audits.append)
        assert check.status is Status.PASS, check.detail
        assert check.facts["duration_s"] >= 1.5 and audits  # waited, auditing while waiting
        assert check.facts["status_after"] == {"NUM_PEERS": "1", "RUNNING": "yes"}
    finally:
        assert dmtcp.rec.cleanup() == []


@linux_only
def test_dmtcp_checkpoint_that_never_lands_fails_safely(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backends, "CHECKPOINT_TIMEOUT_S", 2.0)
    dmtcp, session = fake_checkpoint_setup(tmp_path, "never", monkeypatch)
    try:
        check, _ = dmtcp.checkpoint(session)
        assert check.status is Status.FAIL
        assert "not verified complete" in check.detail and "timed out" in check.detail
        assert "checkpoint_dir_listing" in check.facts
    finally:
        assert dmtcp.rec.cleanup() == []


@linux_only
def test_dmtcp_checkpoint_fails_fast_when_workload_dies(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    dmtcp, session = fake_checkpoint_setup(tmp_path, "never", monkeypatch)
    try:
        os.killpg(session.workload.ident.pid, signal.SIGKILL)
        check, _ = dmtcp.checkpoint(session)
        assert check.status is Status.FAIL and "workload died" in check.detail
        assert check.facts["duration_s"] < 10
    finally:
        assert dmtcp.rec.cleanup() == []
