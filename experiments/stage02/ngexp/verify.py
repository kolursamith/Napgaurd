"""Decide whether a restored workload resumed from checkpointed memory.

Pure functions over observations, so the rules are unit-tested on any host.
For one role, the harness supplies:

* ``original``  observations of the launched process before it was killed;
* ``ckpt_lower`` the highest counter observed *before* the checkpoint request;
* ``ckpt_upper`` the lowest counter observed *after* the checkpoint completed;
* ``kill_counter`` the highest counter observed before the original was killed;
* ``restored``  observations appended after the restore command started;
* ``original_dead`` whether the harness confirmed the original was gone before restoring.

A genuine restore must show the original nonce (not a fresh start), a counter
inside [ckpt_lower, kill_counter) — i.e. rolled back to the checkpoint rather
than continuing or restarting at zero — valid digests, an intact ballast, and
continued progress.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from . import model
from .results import Check, Status


@dataclass(frozen=True)
class Marks:
    ckpt_lower: int
    ckpt_upper: int
    kill_counter: int


def chain_problems(observations: Sequence[model.Observation]) -> list[str]:
    """Digest-chain consistency for observations of one nonce/role."""
    problems = []
    chains: dict[tuple[str, str], model.DigestChain] = {}
    for obs in sorted(observations, key=lambda o: (o.nonce, o.role, o.counter)):
        key = (obs.nonce, obs.role)
        chain = chains.setdefault(key, model.DigestChain(obs.nonce, obs.role))
        if chain.at(obs.counter) != obs.digest:
            problems.append(f"line {obs.line_no}: digest does not match nonce/role/counter")
    return problems


def ballast_problems(
    observations: Sequence[model.Observation], ballast_mib: int
) -> list[str]:
    if ballast_mib <= 0:
        return []
    by_nonce: dict[str, int] = {}
    problems = []
    for obs in observations:
        if obs.ballast is None:
            problems.append(f"line {obs.line_no}: ballast digest missing")
            continue
        expected = by_nonce.setdefault(obs.nonce, model.ballast_digest(obs.nonce, ballast_mib))
        if obs.ballast != expected:
            problems.append(f"line {obs.line_no}: ballast memory differs from expected content")
    return problems


def verify_fresh_control(
    fresh: Sequence[model.Observation], original_nonce: str, role: str = "main"
) -> Check:
    """Evidence of what a fresh start looks like: new nonce, counter from zero."""
    own = [o for o in fresh if o.role == role]
    if not own:
        return Check("fresh-start control", Status.FAIL, "fresh process produced no observations")
    first = min(own, key=lambda o: o.line_no)
    facts = {"fresh_nonce": first.nonce, "fresh_first_counter": first.counter}
    if first.nonce == original_nonce:
        return Check("fresh-start control", Status.FAIL, "fresh process reused the nonce", facts=facts)
    if first.counter != 0:
        return Check(
            "fresh-start control", Status.FAIL, "fresh process did not start at counter 0", facts=facts
        )
    if chain_problems(own):
        return Check("fresh-start control", Status.FAIL, "fresh digests inconsistent", facts=facts)
    return Check(
        "fresh-start control",
        Status.PASS,
        "a fresh start has a new nonce and starts at counter 0",
        facts=facts,
    )


def verify_restore(
    *,
    role: str,
    original: Sequence[model.Observation],
    marks: Marks,
    restored: Sequence[model.Observation],
    original_dead: bool,
    ballast_mib: int = 0,
) -> Check:
    name = f"restored state continuity ({role})"
    orig = [o for o in original if o.role == role]
    rest = sorted((o for o in restored if o.role == role), key=lambda o: o.line_no)
    facts: dict[str, object] = {
        "ckpt_lower": marks.ckpt_lower,
        "ckpt_upper": marks.ckpt_upper,
        "kill_counter": marks.kill_counter,
        "restored_observations": len(rest),
    }

    def fail(reason: str) -> Check:
        return Check(name, Status.FAIL, reason, facts=facts)

    if not orig:
        return fail("no observations of the original process")
    nonces = {o.nonce for o in orig}
    if len(nonces) != 1:
        return fail(f"original observations carry {len(nonces)} nonces")
    nonce = nonces.pop()
    if not (marks.ckpt_lower <= marks.ckpt_upper < marks.kill_counter):
        return fail("marks not ordered: need ckpt_lower <= ckpt_upper < kill_counter")
    if not original_dead:
        return fail("original process was not confirmed terminated before restore")
    if len(rest) < 2:
        return fail("fewer than two observations after restore")

    first, last = rest[0], rest[-1]
    facts.update(
        {
            "nonce": nonce,
            "restored_nonce": first.nonce,
            "restored_first_counter": first.counter,
            "restored_last_counter": last.counter,
            "original_kpid": orig[-1].kpid,
            "restored_kpid": first.kpid,
        }
    )
    if any(o.nonce != nonce for o in rest):
        return fail("nonce changed: this is a fresh start, not a restore")
    problems = chain_problems([*orig, *rest]) + ballast_problems([*orig, *rest], ballast_mib)
    if problems:
        facts["problems"] = problems[:10]
        return fail(f"{len(problems)} state integrity problem(s); first: {problems[0]}")
    if first.counter < marks.ckpt_lower:
        return fail("restored counter is below the pre-checkpoint value (restarted or older state)")
    if first.counter >= marks.kill_counter:
        return fail(
            "restored counter is not below the value at termination: the state did not roll back "
            "to the checkpoint (original may have survived)"
        )
    if last.counter <= first.counter:
        return fail("restored process did not advance after restore")
    rollback = marks.kill_counter - first.counter
    facts["ticks_rolled_back"] = rollback
    return Check(
        name,
        Status.PASS,
        f"same nonce, counter resumed at {first.counter} within checkpoint window "
        f"[{marks.ckpt_lower}, {marks.ckpt_upper}+], {rollback} ticks before termination, "
        f"and advanced to {last.counter}",
        facts=facts,
    )
