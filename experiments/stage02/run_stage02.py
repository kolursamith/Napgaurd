#!/usr/bin/env python3
"""NapGuard Stage 02 checkpoint-engine feasibility harness (Linux only).

    python3 experiments/stage02/run_stage02.py env
    python3 experiments/stage02/run_stage02.py trial --backend dmtcp --class c-single
    python3 experiments/stage02/run_stage02.py matrix --backend dmtcp
    python3 experiments/stage02/run_stage02.py reboot-prepare --backend dmtcp
    python3 experiments/stage02/run_stage02.py reboot-resume --run-dir <dir>

Exit status: 0 PASS, 1 FAIL, 2 BLOCKED, 3 SKIP. See experiments/stage02/README.md.
"""

import sys

from ngexp.cli import main

if __name__ == "__main__":
    sys.exit(main())
