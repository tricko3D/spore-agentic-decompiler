#!/usr/bin/env python3
"""Audit Sail Research ttft_deadline_guard incidents without deleting attempts.

Default is read-only preview. --apply creates a SQLite online backup and then
marks only incontrovertible provider-only passes VOID_PROVIDER. Original rows
and JSONL files remain intact, and void passes do not consume retry allowance.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'work' / 'scratch'))
import spore_sqlite_store as store

LOGS = ROOT / 'work' / 'scratch' / 'codex-pass1' / 'logs'
BACKUPS = ROOT / 'work' / 'scratch' / 'codex-pass1' / 'sqlite-backups'


def worker_pids():
    proc = Path('/proc')
    if not proc.is_dir():
        raise RuntimeError('Cannot check live worker processes without /proc')
    out = []
    for p in proc.iterdir():
        if not p.name.isdigit() or int(p.name) == os.getpid():
            continue
        try:
            argv = [x.decode('utf-8', 'replace') for x in (p / 'cmdline').read_bytes().split(b'\0') if x]
        except (OSError, PermissionError):
            continue
        if any(x.endswith('/spore_first_pass_sqlite.py') or x == 'spore_first_pass_sqlite.py'
               for x in argv):
            out.append(p.name)
    return out


from spore_provider_log import provider_only


def candidates():
    with store.connect() as db:
        existing = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='attempt_reviews'").fetchone()
        if existing:
            query = '''SELECT a.id,a.va,a.status,a.note,a.utc,a.log_path,a.run_id,a.pass_number
              FROM attempts a LEFT JOIN attempt_reviews r ON r.attempt_id=a.id
              WHERE r.attempt_id IS NULL AND a.status IN ('FAIL','DEFER') ORDER BY a.id'''
        else:
            query = '''SELECT id,va,status,note,utc,log_path,run_id,pass_number
              FROM attempts WHERE status IN ('FAIL','DEFER') ORDER BY id'''
        rows = db.execute(query).fetchall()
    matched = []
    not_verified = {}
    for r in rows:
        row = dict(r)
        if not row['log_path']:
            not_verified['no saved log path'] = not_verified.get('no saved log path', 0) + 1
            continue
        path = Path(row['log_path'])
        if not path.is_absolute():
            path = ROOT / path
        ok, evidence = provider_only(path, row['utc'])
        if ok:
            matched.append({**row, 'reason': 'Sail Research ttft_deadline_guard; no model/tool output',
                            'evidence': evidence})
        else:
            not_verified[evidence] = not_verified.get(evidence, 0) + 1
    return matched, not_verified


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--apply', action='store_true', help='Back up SQLite and invalidate provider-only passes')
    ap.add_argument('--show', type=int, default=20, help='How many matching passes to display')
    args = ap.parse_args()
    if not store.DB.exists():
        raise SystemExit(f'SQLite database does not exist: {store.DB}')
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM metadata WHERE key='legacy_import'").fetchone():
            raise SystemExit('Database not migrated from the legacy CSV queue')
    matches, unmatched = candidates()
    print(f'Proven provider-only passes: {len(matches)} across {len(set(x["va"] for x in matches))} functions')
    for x in matches[:args.show]:
        print(f'  #{x["id"]} {x["va"]} pass {x["pass_number"]} [{x["run_id"] or "legacy"}]: {x["evidence"]}')
    if len(matches) > args.show:
        print(f'  ... {len(matches)-args.show} more')
    print('Unverified rows (left intact):', sum(unmatched.values()))
    for k, v in sorted(unmatched.items(), key=lambda kv: -kv[1])[:8]:
        print(f'  {v:4} {k}')
    if not args.apply:
        print('Preview only. No data changed. Use --apply after reviewing.')
        return
    pids = worker_pids()
    if pids:
        raise SystemExit('Refusing mutation: worker PIDs still running: ' + ', '.join(pids))
    if not matches:
        print('Nothing to invalidate.')
        return
    BACKUPS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    destination = BACKUPS / f'spore-before-provider-recovery-{stamp}.sqlite3'
    if destination.exists():
        raise SystemExit('Backup already exists: ' + str(destination))
    with store.connect() as db, sqlite3.connect(destination) as back:
        db.backup(back)
    print('SQLite online backup:', destination)
    # Apply the additive schema upgrade only AFTER preserving the original DB.
    store.init_schema()
    # A killed run can leave stale claims/reservations. They are cleared ONLY
    # after confirming there are no worker processes, before annotations.
    print('Cleared stale claims:', store.clear_stale_claims())
    print('Cleared stale reservations:', store.clear_stale_reservations())
    print('Cleared stale inference slots:', store.clear_stale_inference_slots())
    count = store.mark_provider_only(matches)
    print(f'Annotated {count} provider-only passes VOID_PROVIDER; original rows/logs preserved.')
    print('Recovered pass allowances are reflected in spore_parallel_sqlite.py status.')


if __name__ == '__main__':
    main()
