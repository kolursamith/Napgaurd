"""Tk-free regressions for post-launch selection and event-loop stall diagnostics."""

from __future__ import annotations

import ast
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from napguard.ui.selection import SelectionTracker
from napguard.ui.watchdog import MAX_LOG_BYTES, ResponsivenessWatchdog


class FakeRoot:
    def __init__(self) -> None:
        self.pending: list[Callable[[], None]] = []

    def after(self, _ms: int, func: Callable[..., Any], *args: Any) -> str:
        self.pending.append(lambda: func(*args))
        return "after#1"


def test_selection_preserves_current_row_and_selects_only_genuinely_new_rows() -> None:
    tracker = SelectionTracker()
    assert tracker.choose(["existing"], []) is None
    assert tracker.choose(["existing", "launched"], []) == "launched"
    assert tracker.choose(["existing", "launched", "later"], ["existing"]) == "existing"
    assert tracker.choose(["existing", "launched", "later"], []) is None
    assert tracker.choose(["existing", "launched", "later", "newest"], []) == "newest"


def test_tree_selection_callback_cannot_repopulate_its_own_tree() -> None:
    source_path = Path(__file__).parents[2] / "src" / "napguard" / "ui" / "app.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    processes = next(node for node in tree.body
                     if isinstance(node, ast.ClassDef) and node.name == "ProcessesPage")
    handler = next(node for node in processes.body
                   if isinstance(node, ast.FunctionDef) and node.name == "_selection_changed")
    called = {node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
              for node in ast.walk(handler) if isinstance(node, ast.Call)
              and isinstance(node.func, (ast.Attribute, ast.Name))}
    assert "update_view" not in called
    assert "fill" not in called

    constructor = next(node for node in processes.body
                       if isinstance(node, ast.FunctionDef) and node.name == "__init__")
    binding = [node for node in ast.walk(constructor) if isinstance(node, ast.Call)
               and isinstance(node.func, ast.Attribute) and node.func.attr == "bind"]
    assert any(isinstance(call.args[1], ast.Attribute)
               and call.args[1].attr == "_selection_changed" for call in binding)


def test_watchdog_captures_one_private_bounded_dump_per_stall(tmp_path: Path) -> None:
    root = FakeRoot()
    now = [10.0]
    path = tmp_path / "private" / "gui-hang.log"
    watchdog = ResponsivenessWatchdog(root, path, clock=lambda: now[0],
                                      stall_seconds=2.0, start_thread=False)

    now[0] = 13.0
    assert watchdog.check() is True
    assert watchdog.check() is False
    assert path.stat().st_mode & 0o777 == stat.S_IRUSR | stat.S_IWUSR
    assert b"Tk heartbeat stalled" in path.read_bytes()

    root.pending.pop(0)()  # the Tk thread serviced its scheduled heartbeat
    now[0] = 16.0
    assert watchdog.check() is True
    assert path.stat().st_size <= MAX_LOG_BYTES
    watchdog.stop()
