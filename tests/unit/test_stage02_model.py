"""Stage 02 workload state model and report parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from ngexp import model

NONCE = "0123456789abcdef0fedcba987654321"


def test_splitmix64_matches_reference_vectors() -> None:
    # First outputs of the reference splitmix64 generator seeded with 0.
    assert model.splitmix64(0) == 0xE220A8397B1DCDAF
    assert model.splitmix64(model.GOLDEN_GAMMA) == 0x6E789E6AA1B965F4


def test_digest_chain_is_incremental_and_forward_only() -> None:
    chain = model.DigestChain(NONCE, "main")
    d5 = chain.at(5)
    assert d5 == model.digest_at(NONCE, "main", 5)
    assert chain.at(9) == model.digest_at(NONCE, "main", 9)
    with pytest.raises(ValueError):
        chain.at(4)


def test_roles_have_distinct_chains() -> None:
    digests = {model.digest_at(NONCE, role, 3) for role in ("main", "child", "t0", "t1")}
    assert len(digests) == 4
    with pytest.raises(ValueError):
        model.role_salt("t-1")


def test_parse_line_round_trip() -> None:
    digest = model.digest_at(NONCE, "t2", 7)
    line = (f"NGSTATE v1 run=managed role=t2 pid=40000 kpid=1234 nonce={NONCE} counter=7 "
            f"digest={digest:016x} ballast=- mono_ns=99")
    obs = model.parse_line(line, 3)
    assert obs == model.Observation("managed", "t2", 40000, 1234, NONCE, 7, digest, None, 99, 3)
    assert model.parse_line("unrelated diagnostic output") is None


@pytest.mark.parametrize(
    "line",
    [
        "NGSTATE v2 run=x role=main pid=1 kpid=- nonce=" + NONCE,
        "NGSTATE v1 run=x role=main pid=1 kpid=-",
        f"NGSTATE v1 run=x role=main pid=one kpid=- nonce={NONCE} counter=1 digest=0 "
        "ballast=- mono_ns=1",
        "NGSTATE v1 run=x role=main pid=1 kpid=- nonce=xyz counter=1 digest=0 ballast=- mono_ns=1",
    ],
)
def test_malformed_reports_are_rejected(line: str) -> None:
    with pytest.raises(model.MalformedReport):
        model.parse_line(line)


def test_read_reports_from_offset(tmp_path: Path) -> None:
    lines = [
        f"NGSTATE v1 run=r role=main pid=1 kpid=- nonce={NONCE} counter={c} "
        f"digest={model.digest_at(NONCE, 'main', c):016x} ballast=- mono_ns={c}"
        for c in (0, 10, 20)
    ]
    path = tmp_path / "r.txt"
    path.write_text("\n".join(["noise", *lines]) + "\n", encoding="utf-8")
    assert model.count_lines(path) == 4
    assert [o.counter for o in model.read_reports(path)] == [0, 10, 20]
    assert [o.counter for o in model.read_reports(path, start_line=2)] == [10, 20]
    assert model.read_reports(tmp_path / "missing.txt") == []
