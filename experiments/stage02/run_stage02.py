#!/usr/bin/env python3
"""NapGuard Stage 02 checkpoint-engine feasibility harness (Linux only).

Run with isolated mode (-I), so PYTHONPATH, the user site directory, and the
current directory cannot inject modules:

    python3 -I experiments/stage02/run_stage02.py env
    python3 -I experiments/stage02/run_stage02.py trial --backend criu --class c-single
    python3 -I experiments/stage02/run_stage02.py dmtcp-isolation-check
    python3 -I experiments/stage02/run_stage02.py reboot-prepare --backend criu
    python3 -I experiments/stage02/run_stage02.py reboot-resume --run-dir <dir>

Exit status: 0 PASS, 1 FAIL, 2 BLOCKED, 3 SKIP. See experiments/stage02/README.md.
"""

import sys
from pathlib import Path

# -I removes the script's directory from sys.path. Add back exactly this file's own
# (symlink-resolved) directory, which contains the `ngexp` package, and nothing else.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from ngexp.cli import main  # noqa: E402  (import after the sys.path fix above)

if __name__ == "__main__":
    sys.exit(main())
