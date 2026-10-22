"""Entry point of the NapGuard desktop GUI (see app.py). Needs Tkinter: python3-tk."""

from __future__ import annotations

import sys


def main(socket_path: str, smoke: bool = False) -> int:
    try:
        from .app import main as app_main
    except ImportError as exc:  # Ubuntu: sudo apt install python3-tk
        print(f"The GUI needs Tkinter ({exc}). On Ubuntu: sudo apt install python3-tk",
              file=sys.stderr)
        return 2
    return app_main(socket_path, smoke=smoke)
