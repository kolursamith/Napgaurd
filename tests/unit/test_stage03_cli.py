"""Stage 03a `napguard-dev` CLI: refusals, exit codes, and crash cleanup."""

from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path

import pytest

from napguard import cli
from napguard.core import proc

linux_only = pytest.mark.skipif(platform.system() != "Linux", reason="needs Linux /proc")


def test_selftest_without_the_flag_is_blocked_and_starts_nothing(
        capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    def forbid(*args: object, **kwargs: object) -> None:
        raise AssertionError("no backend may be created without the flag")

    monkeypatch.setattr(cli, "DmtcpBackend", forbid)
    for argv in (["selftest"], ["backend", "selftest", "--class", "c-threads"]):
        assert cli.main(argv) == 2
        assert "No coordinator was started" in capsys.readouterr().err


def test_selftest_only_offers_verified_reference_classes() -> None:
    for cls in ("py", "c-child", "tcp", "gui", "c-file-grow"):
        with pytest.raises(SystemExit):
            cli.main(["selftest", "--i-accept-single-user-coordinator", "--class", cls])


def test_probe_without_dmtcp_is_blocked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["backend", "probe", "--json", "--dmtcp-prefix", str(tmp_path)]) == 2
    data = json.loads(capsys.readouterr().out)
    assert data["dmtcp"]["available"] is False and data["criu"]["available"] is False


def test_assess_refuses_python_and_needs_a_program(tmp_path: Path,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    script = tmp_path / "job"
    script.write_text("#!/usr/bin/env python3\nprint(1)\n", encoding="utf-8")
    script.chmod(0o755)
    assert cli.main(["assess", "--json", "--", str(script)]) == 1
    codes = {f["code"] for f in json.loads(capsys.readouterr().out)["findings"]}
    assert "KNOWN_RESTORE_FAILURE" in codes
    assert cli.main(["backend", "assess"]) == 2


def test_cleanup_refuses_a_non_private_session_dir(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    assert cli.main(["cleanup", "--session-dir", str(shared)]) == 2


@linux_only
def test_cleanup_stops_only_recorded_identities(tmp_path: Path,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    session = tmp_path / "s"
    session.mkdir(mode=0o700)
    bystander = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        info = proc.read_info(bystander.pid)
        assert info is not None
        proc.SessionState(str(session), 4100, [{
            "name": "reused-pid", "pid": bystander.pid,
            "start_ticks": info.ident.start_ticks + 7, "uid": os.getuid(),
            "boot_id": proc.boot_id()}]).write(session / "session.json")
        assert cli.main(["cleanup", "--json", "--session-dir", str(session)]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["stopped"] == [] and len(out["skipped"]) == 1
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()
