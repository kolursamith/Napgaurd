"""Decide whether a restored reference workload resumed from checkpointed memory.

Reviewed port of the Stage 02 continuity rules (`experiments/stage02/ngexp/verify.py`),
which were verified on Ubuntu 24.04 aarch64. Pure functions, unit-tested on any host.

For one role:

* ``original``: observations of the launched process before it was killed;
* ``ckpt_lower``: the highest counter observed before the checkpoint request;
* ``ckpt_upper``: the lowest counter observed after the checkpoint completed;
* ``kill_counter``: the highest counter observed before the original was killed;
* ``restored``: observations appended after the restore started.

A genuine restore shows the ORIGINAL nonce (a new process has a new one), a first
counter inside [ckpt_lower, kill_counter) (rolled back to the checkpoint, not
continuing and not restarting from 0), valid digests, an intact ballast, and progress.
The original must have been confirmed terminated first, so its output cannot be
mistaken for the restored process.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from . import model


class Status(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str
    facts: dict[str, object] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        return {"name": self.name, "status": str(self.status), "detail": self.detail,
                "facts": self.facts}


def overall(checks: Sequence[Check]) -> Status:
    """FAIL beats BLOCKED beats PASS; nothing executed is never PASS."""
    if any(c.status is Status.FAIL for c in checks):
        return Status.FAIL
    if not checks or any(c.status is Status.BLOCKED for c in checks):
        return Status.BLOCKED
    return Status.PASS


@dataclass(frozen=True)
class Marks:
    ckpt_lower: int
    ckpt_upper: int
    kill_counter: int


def chain_problems(observations: Sequence[model.Observation]) -> list[str]:
    problems = []
    chains: dict[tuple[str, str], model.DigestChain] = {}
    for obs in sorted(observations, key=lambda o: (o.nonce, o.role, o.counter)):
        chain = chains.setdefault((obs.nonce, obs.role), model.DigestChain(obs.nonce, obs.role))
        if chain.at(obs.counter) != obs.digest:
            problems.append(f"line {obs.line_no}: digest does not match nonce/role/counter")
    return problems


def ballast_problems(observations: Sequence[model.Observation], ballast_mib: int) -> list[str]:
    if ballast_mib <= 0:
        return []
    expected: dict[str, int] = {}
    problems = []
    for obs in observations:
        if obs.ballast is None:
            problems.append(f"line {obs.line_no}: ballast digest missing")
        elif obs.ballast != expected.setdefault(obs.nonce,
                                                model.ballast_digest(obs.nonce, ballast_mib)):
            problems.append(f"line {obs.line_no}: ballast memory differs from expected content")
    return problems


def verify_restore(*, role: str, original: Sequence[model.Observation], marks: Marks,
                   restored: Sequence[model.Observation], original_dead: bool,
                   ballast_mib: int = 0) -> Check:
    name = f"restored state continuity ({role})"
    orig = [o for o in original if o.role == role]
    rest = sorted((o for o in restored if o.role == role), key=lambda o: o.line_no)
    facts: dict[str, object] = {"ckpt_lower": marks.ckpt_lower, "ckpt_upper": marks.ckpt_upper,
                                "kill_counter": marks.kill_counter,
                                "restored_observations": len(rest)}

    def fail(reason: str) -> Check:
        return Check(name, Status.FAIL, reason, facts)

    if not orig:
        return fail("no observations of the original process")
    nonces = {o.nonce for o in orig}
    if len(nonces) != 1:
        return fail(f"original observations carry {len(nonces)} nonces")
    nonce = nonces.pop()
    if not (marks.ckpt_lower <= marks.ckpt_upper < marks.kill_counter):
        return fail("marks not ordered: need ckpt_lower <= ckpt_upper < kill_counter")
    if marks.ckpt_lower <= 0:
        return fail("checkpoint taken at counter 0: a fresh start could not be told apart")
    if not original_dead:
        return fail("original process was not confirmed terminated before restore")
    if len(rest) < 2:
        return fail("fewer than two observations after restore")
    first, last = rest[0], rest[-1]
    facts.update({"nonce": nonce, "restored_nonce": first.nonce,
                  "restored_first_counter": first.counter, "restored_last_counter": last.counter,
                  "original_kpid": orig[-1].kpid, "restored_kpid": first.kpid})
    if any(o.nonce != nonce for o in rest):
        return fail("nonce changed: this is a new process, not a restore")
    problems = chain_problems([*orig, *rest]) + ballast_problems([*orig, *rest], ballast_mib)
    if problems:
        facts["problems"] = problems[:10]
        return fail(f"{len(problems)} state integrity problem(s); first: {problems[0]}")
    if first.counter < marks.ckpt_lower:
        return fail("restored counter is below the pre-checkpoint value (restarted or older state)")
    if first.counter >= marks.kill_counter:
        return fail("restored counter is not below the value at termination: the state did not "
                    "roll back to the checkpoint (the original may have survived)")
    if last.counter <= first.counter:
        return fail("restored process did not advance after restore")
    facts["ticks_rolled_back"] = marks.kill_counter - first.counter
    return Check(name, Status.PASS,
                 f"same nonce, counter resumed at {first.counter} within the checkpoint window "
                 f"[{marks.ckpt_lower}, {marks.ckpt_upper}+], {marks.kill_counter - first.counter} "
                 f"ticks before termination, and advanced to {last.counter}", facts)


def first_report_latency_s(started_mono_ns: int,
                           restored: Sequence[model.Observation]) -> float | None:
    """Seconds from restore start to the first restored report (PRF-003), or None."""
    if not restored:
        return None
    first = min(o.mono_ns for o in restored)
    return None if first < started_mono_ns else round((first - started_mono_ns) / 1e9, 3)
