"""State model of the NapGuard reference workload (`ngcounter.c`).

Reviewed port of `experiments/stage02/ngexp/model.py`, verified with the same workload
on Ubuntu 24.04 aarch64 in Stage 02. The workload keeps its state only in memory:

* a 128-bit random nonce chosen at startup (a fresh start gets a new one);
* per role (main, worker threads) a counter and a digest chain:
  ``d_0 = splitmix64(nonce_hi ^ nonce_lo ^ salt)``, ``d_k = splitmix64(d_(k-1) ^ k)``;
* optionally a "ballast" buffer filled from the nonce and never modified, whose digest
  is recomputed from memory at every report.

It appends one ``NGSTATE`` line per report to a file it opens and closes each time and
never reads. The self-test reads that file only to OBSERVE the state.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

MASK64 = (1 << 64) - 1
GOLDEN_GAMMA = 0x9E3779B97F4A7C15

SALT_MAIN = 0
SALT_THREAD_BASE = 0x100
SALT_CHILD = 0x200


def splitmix64(x: int) -> int:
    """One splitmix64 output for state ``x`` (identical to the C workload)."""
    z = (x + GOLDEN_GAMMA) & MASK64
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
    return z ^ (z >> 31)


def role_salt(role: str) -> int:
    if role == "main":
        return SALT_MAIN
    if role == "child":
        return SALT_CHILD
    match = re.fullmatch(r"t(\d{1,3})", role)
    if match:
        return SALT_THREAD_BASE + int(match.group(1))
    raise ValueError(f"unknown role {role!r}")


def split_nonce(nonce_hex: str) -> tuple[int, int]:
    if not re.fullmatch(r"[0-9a-f]{32}", nonce_hex):
        raise ValueError(f"bad nonce {nonce_hex!r}")
    return int(nonce_hex[:16], 16), int(nonce_hex[16:], 16)


def initial_digest(nonce_hex: str, role: str) -> int:
    hi, lo = split_nonce(nonce_hex)
    return splitmix64(hi ^ lo ^ role_salt(role))


class DigestChain:
    """Computes chain digests for increasing counters without recomputing from 0."""

    def __init__(self, nonce_hex: str, role: str) -> None:
        self._counter = 0
        self._digest = initial_digest(nonce_hex, role)

    def at(self, counter: int) -> int:
        if counter < self._counter:
            raise ValueError("DigestChain only moves forward; create a new chain")
        while self._counter < counter:
            self._counter += 1
            self._digest = splitmix64(self._digest ^ self._counter)
        return self._digest


def digest_at(nonce_hex: str, role: str, counter: int) -> int:
    return DigestChain(nonce_hex, role).at(counter)


def ballast_digest(nonce_hex: str, mib: int) -> int:
    """Digest of the ballast buffer of ``mib`` MiB for this nonce."""
    hi, lo = split_nonce(nonce_hex)
    h = splitmix64(hi)
    for i in range(mib * 1024 * 1024 // 8):
        h = splitmix64(h ^ splitmix64((lo + i) & MASK64))
    return h


@dataclass(frozen=True)
class Observation:
    run: str
    role: str
    pid: int
    kpid: int | None
    nonce: str
    counter: int
    digest: int
    ballast: int | None
    mono_ns: int
    line_no: int


_FIELD = re.compile(r"(\w+)=(\S+)")
_REQUIRED = ("run", "role", "pid", "kpid", "nonce", "counter", "digest", "ballast", "mono_ns")


class MalformedReport(ValueError):
    pass


def parse_line(line: str, line_no: int = 0) -> Observation | None:
    """Parse one report line; ``None`` for lines that are not NGSTATE reports."""
    if not line.startswith("NGSTATE "):
        return None
    head, _, rest = line.strip().partition(" ")
    version, _, rest = rest.partition(" ")
    if version != "v1":
        raise MalformedReport(f"line {line_no}: unsupported version {version!r}")
    fields = dict(_FIELD.findall(rest))
    missing = [name for name in _REQUIRED if name not in fields]
    if missing:
        raise MalformedReport(f"line {line_no}: missing {missing}")
    try:
        split_nonce(fields["nonce"])
        return Observation(
            run=fields["run"],
            role=fields["role"],
            pid=int(fields["pid"]),
            kpid=None if fields["kpid"] == "-" else int(fields["kpid"]),
            nonce=fields["nonce"],
            counter=int(fields["counter"]),
            digest=int(fields["digest"], 16),
            ballast=None if fields["ballast"] == "-" else int(fields["ballast"], 16),
            mono_ns=int(fields["mono_ns"]),
            line_no=line_no,
        )
    except ValueError as exc:
        raise MalformedReport(f"line {line_no}: {exc}") from exc


def parse_lines(lines: Iterable[str], first_line_no: int = 1) -> list[Observation]:
    found = []
    for offset, line in enumerate(lines):
        obs = parse_line(line, first_line_no + offset)
        if obs is not None:
            found.append(obs)
    return found


def read_reports(path: Path, start_line: int = 0) -> list[Observation]:
    """Observations from line index ``start_line`` onward (0-based)."""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return parse_lines(lines[start_line:], first_line_no=start_line + 1)


def count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
