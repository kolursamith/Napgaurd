"""Result vocabulary shared by every Stage 02 experiment."""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any


class Status(enum.StrEnum):
    PASS = "PASS"  # executed and met every criterion
    FAIL = "FAIL"  # executed and did not meet a criterion
    SKIP = "SKIP"  # deliberately not executed (not applicable / not implemented), with reason
    BLOCKED = "BLOCKED"  # could not execute: backend missing, privilege absent, prerequisite failed


EXIT_CODES = {Status.PASS: 0, Status.FAIL: 1, Status.BLOCKED: 2, Status.SKIP: 3}


@dataclass
class Check:
    name: str
    status: Status
    detail: str
    mandatory: bool = True
    facts: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = str(self.status)
        return data


def overall(checks: Iterable[Check]) -> Status:
    """Combine checks. Only mandatory checks decide; PASS needs at least one executed PASS.

    Any mandatory FAIL -> FAIL; else any mandatory BLOCKED -> BLOCKED;
    else any mandatory PASS (and the rest PASS/SKIP) -> PASS; else SKIP.
    A run with no executed mandatory check is never reported as PASS.
    """
    mandatory = [c.status for c in checks if c.mandatory]
    if Status.FAIL in mandatory:
        return Status.FAIL
    if Status.BLOCKED in mandatory:
        return Status.BLOCKED
    if Status.PASS in mandatory:
        return Status.PASS
    return Status.SKIP
