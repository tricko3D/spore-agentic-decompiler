#!/usr/bin/env python3
"""Preview or append confidently mapped functions for selected Spore modules.

Existing SQLite queue and attempts are never replaced. Original CSV snapshots
remain immutable, and changes are backed up before any queue expansion.
"""
from datetime import datetime, timezone
import argparse
from pathlib import Path
import sqlite3
import sys

ROOT = Path.cwd()
# Preserve stdlib module precedence (notably `dis` on Python 3.14).
# The upstream checkout also has work/scratch/dis.py, which otherwise shadows it.
sys.path.append(str(ROOT/'work/scratch'))
import spore_first_pass_sqlite as agent
import spore_sqlite_store as store


def candidates():
    modules = agent.ModuleIndex().refresh()
    task = agent.task_module()
    manifests = agent.ManifestIndex().refresh()
    exclude = set(task.blocked()) | set(task.library_functions()) | set(task.marked()) | {int(v, 16) for v in manifests}
    rows = []
    by_module = {}
    for va, fp in task.fingerprints().items():
        if va in exclude:
            continue
        name, conf = modules.classification(va)
        if name and conf is not None and conf >= modules.minimum:
            top = name.split('/', 1)[0]
            by_module[top] = by_module.get(top, 0) + 1
            if modules.eligible(va):
                rows.append((f'0x{va:08x}', int(fp['size']), task.new_source_path(va)))
    return rows, by_module, modules


def main():
    ap = argparse.ArgumentParser(description='Safely extend a Spore module queue')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--list-modules', action='store_true')
    args = ap.parse_args()
    store.ensure_imported()
    store.assert_legacy_unchanged()
    rows, by_module, modules = candidates()
    if args.list_modules:
        print('Discovered modules and confidently mapped unmatched functions:')
        for name, count in sorted(by_module.items(), key=lambda x: (-x[1], x[0].casefold())):
            print(f'  {name}: {count:,}')
        return 0
    with store.connect() as db:
        if db.execute('SELECT 1 FROM claims LIMIT 1').fetchone():
            sys.exit('Active function claims found. Stop workers before modifying scope.')
        existing = {r['va'] for r in db.execute('SELECT va FROM queue')}
    addition = sorted((r for r in rows if r[0] not in existing), key=lambda r: (r[1], int(r[0], 16)))
    print(f'Selected module scope: {agent.module_scope()} (>= {modules.minimum:.2f} confidence)')
    print(f'Confident unmatched functions: {len(rows):,} | Already queued: {len(rows)-len(addition):,} | New: {len(addition):,}')
    if not args.apply:
        print('Preview only. Use scope --apply to append new function targets.')
        return 0
    if not rows:
        sys.exit('No eligible functions for selected scope. Refusing empty scope selection.')
    if not addition:
        print('All selected functions already queued; history unchanged.')
        return 0
    folder = store.WORK/'sqlite-backups'
    folder.mkdir(parents=True, exist_ok=True)
    backup = folder / ('queue-before-scope-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.sqlite3')
    with sqlite3.connect(store.DB) as source, sqlite3.connect(backup) as dest:
        source.backup(dest)
    print('Database backup:', backup)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            if db.execute('SELECT 1 FROM claims LIMIT 1').fetchone():
                raise RuntimeError('Active claims appeared during queue extension')
            position = db.execute('SELECT COALESCE(MAX(position),0) FROM queue').fetchone()[0]
            db.executemany('INSERT OR IGNORE INTO queue(va,position,size,suggested_file) VALUES(?,?,?,?)',
                           ((va, position+i, size, suggested) for i, (va, size, suggested) in enumerate(addition, 1)))
            db.execute('COMMIT')
        except Exception:
            db.execute('ROLLBACK')
            raise
    print(f'Added {len(addition):,} targets without touching {len(existing):,} existing queue entries or attempts.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
