"""Stage 02 restore-continuity rules on synthetic observation sequences."""

from __future__ import annotations

import dataclasses

from ngexp import model, verify
from ngexp.results import Check, Status, overall

NONCE = "a" * 16 + "b" * 16
OTHER = "c" * 32


def obs(counter: int, line: int, nonce: str = NONCE, role: str = "main",
        digest: int | None = None, ballast: int | None = None) -> model.Observation:
    value = model.digest_at(nonce, role, counter) if digest is None else digest
    return model.Observation("r", role, 100, 100, nonce, counter, value, ballast, counter, line)


ORIGINAL = [obs(c, i) for i, c in enumerate([0, 10, 20, 30, 40, 50, 60, 70, 80], start=1)]
MARKS = verify.Marks(ckpt_lower=30, ckpt_upper=40, kill_counter=80)


def restored(*counters: int, nonce: str = NONCE) -> list[model.Observation]:
    return [obs(c, 100 + i, nonce=nonce) for i, c in enumerate(counters)]


def check(rest: list[model.Observation], **overrides: object) -> Check:
    kwargs: dict[str, object] = {"role": "main", "original": ORIGINAL, "marks": MARKS,
                                 "restored": rest, "original_dead": True}
    kwargs.update(overrides)
    return verify.verify_restore(**kwargs)  # type: ignore[arg-type]


def test_genuine_restore_passes() -> None:
    result = check(restored(40, 50, 60))
    assert result.status is Status.PASS
    assert result.facts["ticks_rolled_back"] == 40


def test_fresh_start_is_detected_by_new_nonce() -> None:
    result = check(restored(0, 10, nonce=OTHER))
    assert result.status is Status.FAIL
    assert "fresh start" in result.detail


def test_counter_reset_with_same_nonce_fails() -> None:
    assert check(restored(0, 10)).status is Status.FAIL


def test_state_not_rolled_back_fails() -> None:
    result = check(restored(80, 90))
    assert result.status is Status.FAIL
    assert "roll back" in result.detail


def test_tampered_digest_fails() -> None:
    rest = restored(40, 50)
    rest[1] = dataclasses.replace(rest[1], digest=rest[1].digest ^ 1)
    assert check(rest).status is Status.FAIL


def test_restore_without_progress_or_observations_fails() -> None:
    assert check(restored(40, 40)).status is Status.FAIL
    assert check(restored(40)).status is Status.FAIL
    assert check([]).status is Status.FAIL


def test_original_not_confirmed_dead_fails() -> None:
    result = check(restored(40, 50), original_dead=False)
    assert result.status is Status.FAIL
    assert "not confirmed terminated" in result.detail


def test_inconsistent_marks_fail() -> None:
    bad = verify.Marks(ckpt_lower=50, ckpt_upper=40, kill_counter=80)
    assert check(restored(40, 50), marks=bad).status is Status.FAIL


def test_ballast_must_match_nonce_derived_memory() -> None:
    good = model.ballast_digest(NONCE, 1)
    orig = [dataclasses.replace(o, ballast=good) for o in ORIGINAL]
    rest = [dataclasses.replace(o, ballast=good) for o in restored(40, 50)]
    assert check(rest, original=orig, ballast_mib=1).status is Status.PASS
    rest[1] = dataclasses.replace(rest[1], ballast=good ^ 1)
    assert check(rest, original=orig, ballast_mib=1).status is Status.FAIL


def test_fresh_control_distinguishes_new_process() -> None:
    fresh = [obs(c, i, nonce=OTHER) for i, c in enumerate([0, 10])]
    assert verify.verify_fresh_control(fresh, NONCE).status is Status.PASS
    assert verify.verify_fresh_control(fresh, OTHER).status is Status.FAIL
    assert verify.verify_fresh_control([], NONCE).status is Status.FAIL


def test_overall_never_passes_without_an_executed_pass() -> None:
    def c(status: Status, mandatory: bool = True) -> Check:
        return Check("x", status, "", mandatory=mandatory)

    assert overall([c(Status.PASS), c(Status.SKIP)]) is Status.PASS
    assert overall([c(Status.PASS), c(Status.BLOCKED)]) is Status.BLOCKED
    assert overall([c(Status.BLOCKED), c(Status.FAIL)]) is Status.FAIL
    assert overall([c(Status.SKIP)]) is Status.SKIP
    assert overall([c(Status.PASS, mandatory=False)]) is Status.SKIP
    assert overall([]) is Status.SKIP
