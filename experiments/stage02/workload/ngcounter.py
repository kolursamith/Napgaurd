#!/usr/bin/env python3
"""NapGuard Stage 02 Python test workload (CMP-001 "Python computation" class).

Same in-memory state and NGSTATE report format as ngcounter.c, single role
("main"). Uses only the standard library and deliberately does not import
ngexp, so the workload is self-contained. It never reads the report file or
any saved state, and has no persistence or recovery code.

    python3 ngcounter.py --report PATH [--label L] [--tick-ms N]
                         [--report-every N] [--ballast-mib N]
"""

from __future__ import annotations

import argparse
import os
import signal
import struct
import time

MASK64 = (1 << 64) - 1


def splitmix64(x: int) -> int:
    z = (x + 0x9E3779B97F4A7C15) & MASK64
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
    return z ^ (z >> 31)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--label", default="unlabeled")
    parser.add_argument("--tick-ms", type=int, default=10)
    parser.add_argument("--report-every", type=int, default=20)
    parser.add_argument("--ballast-mib", type=int, default=0)
    args = parser.parse_args()

    nonce_hi, nonce_lo = struct.unpack("<QQ", os.urandom(16))
    ballast = bytearray()
    if args.ballast_mib > 0:
        words = args.ballast_mib * 1024 * 1024 // 8
        ballast = bytearray(struct.pack(f"<{words}Q",
                                        *(splitmix64((nonce_lo + i) & MASK64)
                                          for i in range(words))))

    stop = False

    def on_term(signum: int, frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    counter = 0
    digest = splitmix64(nonce_hi ^ nonce_lo ^ 0)

    def report() -> None:
        if ballast:
            h = splitmix64(nonce_hi)
            for (word,) in struct.iter_unpack("<Q", ballast):
                h = splitmix64(h ^ word)
            ballast_hex = f"{h:016x}"
        else:
            ballast_hex = "-"
        line = (
            f"NGSTATE v1 run={args.label} role=main pid={os.getpid()} kpid=- "
            f"nonce={nonce_hi:016x}{nonce_lo:016x} counter={counter} digest={digest:016x} "
            f"ballast={ballast_hex} mono_ns={time.monotonic_ns()}\n"
        )
        fd = os.open(args.report, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)

    report()
    while not stop:
        time.sleep(args.tick_ms / 1000)
        counter += 1
        digest = splitmix64(digest ^ counter)
        if counter % args.report_every == 0:
            report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
