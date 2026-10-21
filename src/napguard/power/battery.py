"""Stage 08 (MVP): battery state while Linux is awake, plus LABELLED simulated readings.

Real readings come only from /sys/class/power_supply (read-only, no root; PLT-005). A
UTM VM usually exposes no battery: the state is then reported unavailable and battery
triggers stay inactive (FR-019). A simulated reading exists only for demonstrations; it
is always marked `source: SIMULATED` and every event it causes is flagged simulated
(EXC-009). Nothing here runs or claims to run during suspend (LIM-005).
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

POWER_SUPPLY = Path("/sys/class/power_supply")
HYSTERESIS = 5


@dataclass(frozen=True)
class BatteryReading:
    available: bool
    source: str  # "SYSFS", "SIMULATED", or "NONE"
    percent: int | None = None
    charging: bool | None = None
    ac_online: bool | None = None
    batteries: list[str] = field(default_factory=list)
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self) | {"simulated": self.source == "SIMULATED"}


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def read_power_supply(root: Path = POWER_SUPPLY) -> BatteryReading:
    """Zero, one, or several batteries and an optional AC adapter (FR-017)."""
    if not root.is_dir():
        return BatteryReading(False, "NONE", detail=f"{root} not present")
    capacities, charging, ac, names = [], False, None, []
    for supply in sorted(root.iterdir()):
        kind = _read(supply / "type")
        if kind == "Battery" and _read(supply / "present") in (None, "1"):
            cap = _read(supply / "capacity")
            if cap is not None and cap.isdigit():
                capacities.append(int(cap))
                names.append(supply.name)
            charging = charging or _read(supply / "status") in ("Charging", "Full")
        elif kind in ("Mains", "USB"):
            ac = bool(ac) or _read(supply / "online") == "1"
    if not capacities:
        return BatteryReading(False, "NONE", ac_online=ac,
                              detail="no battery exposed (typical for a UTM VM)")
    return BatteryReading(True, "SYSFS", round(sum(capacities) / len(capacities)), charging, ac,
                          names)


class BatteryMonitor:
    """Low/critical triggers with hysteresis: each fires at most once per discharge (FR-018)."""

    def __init__(self, root: Path = POWER_SUPPLY) -> None:
        self.root = root
        self._simulated: BatteryReading | None = None
        self._armed = {"low": True, "critical": True}
        self._lock = threading.Lock()

    def simulate(self, percent: int, ac_online: bool) -> BatteryReading:
        if not 0 <= percent <= 100:
            raise ValueError("percent must be 0-100")
        with self._lock:
            self._simulated = BatteryReading(True, "SIMULATED", percent, ac_online, ac_online,
                                             ["simulated"], "SIMULATED for demonstration; not a "
                                             "real battery reading")
            return self._simulated

    def clear_simulation(self) -> None:
        with self._lock:
            self._simulated = None
            self._armed = {"low": True, "critical": True}

    def current(self) -> BatteryReading:
        with self._lock:
            sim = self._simulated
        return sim if sim is not None else read_power_supply(self.root)

    def evaluate(self, low: int, critical: int) -> tuple[BatteryReading, list[str]]:
        """Return the reading and the triggers that fire now ("critical", "low")."""
        reading = self.current()
        fired: list[str] = []
        with self._lock:
            if not reading.available or reading.percent is None:
                return reading, fired
            pct = reading.percent
            if reading.ac_online or reading.charging or pct >= low + HYSTERESIS:
                self._armed = {"low": True, "critical": True}
                return reading, fired
            if pct <= critical and self._armed["critical"]:
                fired.append("critical")
                self._armed = {"low": False, "critical": False}
            elif pct <= low and self._armed["low"]:
                fired.append("low")
                self._armed["low"] = False
        return reading, fired
