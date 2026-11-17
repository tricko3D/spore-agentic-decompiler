#!/usr/bin/env python3
"""Initialize a new SPGonzago-only queue using spore-decomp's own indexes.

Refuses to overwrite any existing queue, result history, or SQLite database.
"""
import csv
import os
from pathlib import Path
import sys

ROOT = Path.cwd()
WORK = ROOT / 'work' / 'scratch' / 'codex-pass1'
QUEUE = WORK / 'queue.csv'
RESULTS = WORK / 'results.csv'
DB = WORK / 'spore.sqlite3'
sys.path.insert(0, str(ROOT / 'work' / 'scratch'))
import spore_first_pass_sqlite as agent


def main():
    if QUEUE.exists() or RESULTS.exists() or DB.exists():
        raise SystemExit('Existing queue/results/database found. Refusing to overwrite historical work.')
    if not (ROOT / 'tools/contrib/task.py').is_file():
        raise SystemExit('Not a spore-decomp checkout')
    modules = agent.ModuleIndex()
    modules.refresh()
    task = agent.task_module()
    manifested = {int(x, 16) for x in agent.ManifestIndex().refresh()}
    exclude = set(task.blocked()) | task.library_functions() | task.marked() | manifested
    rows = []
    for va, fingerprint in task.fingerprints().items():
        size = int(fingerprint['size'])
        if va in exclude or not modules.eligible(f'0x{va:08x}'):
            continue
        rows.append({'va': f'0x{va:08x}', 'size': size,
                     'suggested_file': task.new_source_path(va)})
    rows.sort(key=lambda row: (int(row['size']), int(row['va'], 16)))
    if not rows:
        raise SystemExit('No eligible SPGonzago functions. Check src/game or modmap; nothing was created.')
    WORK.mkdir(parents=True, exist_ok=True)
    try:
        for file, fields, data in (
            (QUEUE, ['va','size','suggested_file'], rows),
            (RESULTS, ['va','status','effort','note','utc'], []),
        ):
            with file.open('x', encoding='utf-8', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                writer.writerows(data)
    except Exception:
        # Don't leave half a freshly created queue as an apparently valid setup.
        for file in (QUEUE, RESULTS):
            if file.exists():
                file.unlink()
        raise
    print(f'Created a new SPGonzago queue with {len(rows):,} targets. No sources modified.')


if __name__ == '__main__':
    main()
