"""Regression checks for test-suite behavior across supported pytest versions."""

from __future__ import annotations

import ast
from pathlib import Path


def test_modules_do_not_define_legacy_nose_setup_hooks() -> None:
    """Plain setup/teardown names were hooks before pytest 8, not ordinary helpers."""
    tests_dir = Path(__file__).parents[1]
    offenders: list[str] = []
    for path in sorted(tests_dir.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
                "setup",
                "teardown",
            }:
                offenders.append(f"{path.relative_to(tests_dir)}:{node.lineno}:{node.name}")

    assert offenders == [], "legacy nose hooks found: " + ", ".join(offenders)
