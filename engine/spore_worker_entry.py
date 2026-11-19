#!/usr/bin/env python3
"""Safe entrypoint for the Spore SQLite worker on Python 3.10+.

Launching a script inside work/scratch directly prepends that directory to
sys.path. The checkout's work/scratch/dis.py can then shadow the Python
stdlib dis module (used by argparse on Python 3.14). This root-level entrypoint
preserves stdlib precedence while keeping the runner's original argv and cwd.
"""
from __future__ import annotations

from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parent
SCRATCH = ROOT / "work" / "scratch"
RUNNER = SCRATCH / "spore_first_pass_sqlite.py"


def main():
    if Path.cwd().resolve() != ROOT:
        raise SystemExit(f"Launch from Spore root: {ROOT}")
    if not RUNNER.is_file():
        raise SystemExit(f"Missing installed worker: {RUNNER}")
    # Never prepend this directory. It contains a file named dis.py.
    sys.path.append(str(SCRATCH))
    sys.argv = [str(RUNNER), *sys.argv[1:]]
    runpy.run_path(str(RUNNER), run_name="__main__")


if __name__ == "__main__":
    main()
