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


# ------------------------------------------------------------- held-file classes (LIM-007)


def held_file_lines(text: str) -> tuple[str | None, list[model.Observation], int]:
    """(header line, parseable NGSTATE lines, count of torn/unparseable lines)."""
    lines = text.splitlines()
    header = lines[0] if lines and lines[0].startswith("NGHOLD v1 ") else None
    parsed: list[model.Observation] = []
    torn = 0
    for number, line in enumerate(lines[1 if header else 0 :], start=2 if header else 1):
        try:
            obs = model.parse_line(line, number)
        except model.MalformedReport:
            obs = None
        if obs is None:
            torn += 1
        else:
            parsed.append(obs)
    return header, parsed, torn


def verify_held_file(*, class_name: str, nonce: str, at_checkpoint: dict[str, object],
                     at_kill: dict[str, object], after_restore: dict[str, object],
                     text_after: str) -> Check:
    """c-file-static: the open file must be byte-identical across checkpoint and restore.

    c-file-grow: the workload keeps appending through an fd opened without O_APPEND, so
    what DMTCP does with the file offset on restore is RECORDED (LIM-007), not judged.
    """
    header, parsed, torn = held_file_lines(text_after)
    drops = [(a.counter, b.counter) for a, b in zip(parsed, parsed[1:], strict=False) if b.counter < a.counter]
    facts: dict[str, object] = {
        "at_checkpoint": at_checkpoint, "at_kill": at_kill, "after_restore": after_restore,
        "header_ok": header == f"NGHOLD v1 nonce={nonce}", "ngstate_lines": len(parsed),
        "torn_lines": torn, "counter_drops": drops[:10],
        "foreign_nonce_lines": sum(1 for o in parsed if o.nonce != nonce),
    }
    if class_name == "c-file-static":
        unchanged = (after_restore.get("sha256") == at_checkpoint.get("sha256")
                     == at_kill.get("sha256") and bool(after_restore.get("exists")))
        ok = unchanged and facts["header_ok"] and not parsed and not torn
        return Check("held file consistency (c-file-static)", Status.PASS if ok else Status.FAIL,
                     "held file byte-identical at checkpoint, at termination and after restore"
                     if ok else "held file changed or header lost across checkpoint/restore",
                     facts=facts)
    if after_restore.get("size") == at_kill.get("size") and not drops:
        observed = "file not written after restore"
    elif drops and torn:
        observed = ("restored writes landed at the checkpoint-time offset, overwriting "
                    "post-checkpoint data (torn lines present)")
    elif drops:
        observed = "restored writes resumed at the checkpoint-time offset (counter rewind)"
    else:
        observed = "restored writes appended after the pre-termination data"
    return Check("held file behaviour (c-file-grow, LIM-007 observation)", Status.PASS,
                 observed, mandatory=False, facts=facts)


# ---------------------------------------------------------------------- PRF-003 timing

RESTORE_TIMING_DEFINITION = (
    "restore_to_first_report_s = mono_ns of the earliest restored NGSTATE report minus "
    "time.monotonic_ns() taken immediately before the restore command (both CLOCK_MONOTONIC, "
    "same boot); restore_to_three_reports_upper_bound_s = wall time from restore start until "
    "the harness had seen >= 3 restored reports per role, adopted them and audited listeners"
)


def first_report_latency_s(started_mono_ns: int,
                           restored: Sequence[model.Observation]) -> float | None:
    """Seconds from restore start to the first restored report, or None if not measurable."""
    if not restored:
        return None
    first = min(o.mono_ns for o in restored)
    if first < started_mono_ns:
        return None  # not the same clock/boot: refuse to report a number
    return round((first - started_mono_ns) / 1e9, 3)
