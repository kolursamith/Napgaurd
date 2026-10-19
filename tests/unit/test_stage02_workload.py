"""The Stage 02 workloads agree with the Python state model.

These run the workloads as ordinary processes (no checkpoint engine is
involved) to prove the C program, the Python program, and the verifier
compute the same in-memory state.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ngexp import model

STAGE02 = Path(__file__).resolve().parents[2] / "experiments" / "stage02"


def run_for(argv: list[str], seconds: float) -> None:
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        time.sleep(seconds)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.returncode is None:
            # Kill the test's own process group (it has its own session) while the leader is
            # unreaped, so a forked child cannot be orphaned and no other group can be hit.
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()


def assert_consistent(report: Path, roles: set[str], ballast_mib: int) -> None:
    observations = model.read_reports(report)
    assert {o.role for o in observations} == roles
    assert len({o.nonce for o in observations}) == 1
    for o in observations:
        assert model.digest_at(o.nonce, o.role, o.counter) == o.digest
    for role in roles:
        counters = [o.counter for o in observations if o.role == role]
        assert counters[0] == 0 and counters == sorted(counters) and counters[-1] > 0
    if ballast_mib:
        expected = model.ballast_digest(observations[0].nonce, ballast_mib)
        assert {o.ballast for o in observations} == {expected}


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler")
def test_c_workload_matches_model(tmp_path: Path) -> None:
    binary = tmp_path / "ngcounter"
    subprocess.run(["cc", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread", "-o", str(binary),
                    str(STAGE02 / "workload" / "ngcounter.c")], check=True, timeout=120)
    report = tmp_path / "report.txt"
    run_for([str(binary), "--report", str(report), "--tick-ms", "2", "--report-every", "5",
             "--threads", "2", "--fork-child", "--ballast-mib", "1"], 0.8)
    assert_consistent(report, {"main", "t0", "t1", "child"}, ballast_mib=1)
    observations = model.read_reports(report)
    child_pids = {o.pid for o in observations if o.role == "child"}
    assert child_pids.isdisjoint({o.pid for o in observations if o.role == "main"})


def test_python_workload_matches_model(tmp_path: Path) -> None:
    report = tmp_path / "report.txt"
    run_for([sys.executable, str(STAGE02 / "workload" / "ngcounter.py"), "--report", str(report),
             "--tick-ms", "2", "--report-every", "5", "--ballast-mib", "1"], 1.5)
    assert_consistent(report, {"main"}, ballast_mib=1)


def test_workloads_never_read_their_report_file() -> None:
    """No code path loads saved progress: the report path is only opened write-only/append."""
    c_src = (STAGE02 / "workload" / "ngcounter.c").read_text(encoding="utf-8")
    py_src = (STAGE02 / "workload" / "ngcounter.py").read_text(encoding="utf-8")
    for forbidden in ("O_RDONLY", "O_RDWR", "fopen", "read(", "fread", "mmap"):
        assert forbidden not in c_src, forbidden
    for forbidden in ("open(args.report, \"r", ".read(", "O_RDONLY", "O_RDWR", "pickle", "json"):
        assert forbidden not in py_src, forbidden
