"""Tk-free selection policy for refreshable workload tables."""

from __future__ import annotations

from collections.abc import Sequence


class SelectionTracker:
    """Preserve a valid selection and identify genuinely new rows after a refresh."""

    def __init__(self) -> None:
        self._known: set[str] = set()
        self._initialized = False

    def choose(self, current: Sequence[str], selected: Sequence[str]) -> str | None:
        current_set = set(current)
        preserved = next((item for item in selected if item in current_set), None)
        newly_seen = next((item for item in current if item not in self._known), None)
        self._known = current_set
        was_initialized, self._initialized = self._initialized, True
        if preserved is not None:
            return preserved
        return newly_seen if was_initialized else None
