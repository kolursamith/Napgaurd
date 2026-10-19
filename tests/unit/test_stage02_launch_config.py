"""Regression tests for the Stage 02 Ubuntu findings (2026-10-19, commit 4879432).

1. `python3 -I experiments/stage02/run_stage02.py ...` failed: `ngexp` was not importable,
   because isolated mode drops the script's directory from sys.path.
2. Plain `python3 -m pytest -q` failed on a fresh checkout: `napguard` (src layout) was
   not importable without installing the package or setting PYTHONPATH.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import napguard

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "experiments" / "stage02" / "run_stage02.py"


def clean_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env


def test_launcher_works_in_isolated_mode_from_any_directory(tmp_path: Path) -> None:
    done = subprocess.run([sys.executable, "-I", str(LAUNCHER), "--help"], cwd=tmp_path,
                          env=clean_env(), capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "dmtcp-isolation-check" in done.stdout


def test_launcher_does_not_trust_the_current_directory(tmp_path: Path) -> None:
    # A planted `ngexp` in the working directory must not shadow the real package.
    (tmp_path / "ngexp").mkdir()
    (tmp_path / "ngexp" / "__init__.py").write_text("raise SystemExit('planted ngexp ran')\n")
    done = subprocess.run([sys.executable, "-I", str(LAUNCHER), "--help"], cwd=tmp_path,
                          env=clean_env(), capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "planted" not in done.stdout + done.stderr


def test_pytest_config_puts_src_and_experiments_on_path() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["tool"]["pytest"]["ini_options"]["pythonpath"] == ["src",
                                                                     "experiments/stage02"]
    paths = {Path(p).resolve() for p in sys.path if p}
    assert ROOT / "src" in paths
    assert ROOT / "experiments" / "stage02" in paths


def test_napguard_under_test_is_the_checkout_source() -> None:
    # With `pythonpath = ["src", ...]` the tests exercise this checkout's source even when
    # no package is installed (the failure seen with plain `python3 -m pytest` on Ubuntu).
    assert Path(napguard.__file__).resolve().is_relative_to(ROOT / "src")
