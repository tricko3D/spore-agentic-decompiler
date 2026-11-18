#!/usr/bin/env python3
"""Python-safe entrypoint for cloned SAD Brain benchmark commands.

Do NOT run work/scratch scripts as Python file entrypoints: Spore has a
work/scratch/dis.py which shadows Python's own stdlib 'dis' in Python 3.14.
Starting from the workspace root and appending scratch to sys.path means
stdlib and site-packages have precedence over Spore's helper scripts.
This launcher executes the original script without modifying its file or
the production checkout.
"""
from __future__ import annotations

from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parent
SCRATCH = ROOT / "work/scratch"
SCRIPT = {
    "runner": "spore_first_pass_sqlite.py",
    "query": "brain_trial_query.py",
    "memory": "spore_collective_memory.py",
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in SCRIPT:
        raise SystemExit("Usage: python brain_trial_entry.py {runner|query|memory} [arguments...]")
    if Path.cwd().resolve() != ROOT:
        raise SystemExit("Run the benchmark entrypoint from its own cloned repo root")
    if not SCRATCH.is_dir():
        raise SystemExit("Benchmark work/scratch scripts missing")
    target = SCRATCH / SCRIPT[sys.argv[1]]
    if not target.is_file():
        raise SystemExit(f"Missing benchmark script: {target}")
    # IMPORTANT: never insert scratch at sys.path[0], or it will shadow Python.
    sys.path.append(str(SCRATCH))
    sys.argv = [str(target), *sys.argv[2:]]
    runpy.run_path(str(target), run_name="__main__")


if __name__ == "__main__":
    main()
