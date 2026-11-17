#!/usr/bin/env python3
"""Read-only Spore byte-exact progress from the parallel SQLite runner.

Shows:
  * All-time matches across the 930-function queue.
  * Cumulative matches within the SPGonzago scope assigned to the selected run.
  * First-time matches and retail bytes gained during that parallel run.

The most recent run is selected by default. To inspect an earlier batch, pass
--run-id. No OpenRouter calls, compiler calls, or database writes are made.

Examples:
    .venv/bin/python spore_matched_bytes.py
    .venv/bin/python spore_matched_bytes.py --watch 10
    .venv/bin/python spore_matched_bytes.py --run-id 20261117-164107-216650
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3
import sys
import time
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / 'work' / 'scratch' / 'codex-pass1' / 'spore.sqlite3'

# Latest outcome for every VA. The existing runner treats the latest attempt as
# authoritative; a previous FAIL must not override a later MATCH, or vice versa.
LATEST = """
WITH valid_attempts AS (
    SELECT a.* FROM attempts a
    LEFT JOIN attempt_reviews r ON r.attempt_id=a.id
    WHERE r.attempt_id IS NULL
),
latest AS (
    SELECT a.va, a.status
    FROM valid_attempts a
    JOIN (SELECT va, MAX(id) AS last_id FROM valid_attempts GROUP BY va) m
      ON a.id = m.last_id
),
first_match AS (
    SELECT va, MIN(id) AS first_id
    FROM valid_attempts
    WHERE status = 'MATCH'
    GROUP BY va
),
first_match_run AS (
    SELECT fm.va, a.run_id
    FROM first_match fm JOIN valid_attempts a ON a.id = fm.first_id
)
"""

ALL_SUMMARY = LATEST + """
SELECT COUNT(*) AS total_functions,
       COALESCE(SUM(q.size), 0) AS total_bytes,
       COALESCE(SUM(CASE WHEN l.status='MATCH' THEN 1 ELSE 0 END), 0) AS matched_functions,
       COALESCE(SUM(CASE WHEN l.status='MATCH' THEN q.size ELSE 0 END), 0) AS matched_bytes,
       COALESCE(SUM(CASE WHEN l.status IS NOT NULL THEN 1 ELSE 0 END), 0) AS attempted_functions
FROM queue q LEFT JOIN latest l ON l.va=q.va
"""

SCOPE_SUMMARY = LATEST + """
SELECT COUNT(*) AS total_functions,
       COALESCE(SUM(q.size), 0) AS total_bytes,
       COALESCE(SUM(CASE WHEN l.status='MATCH' THEN 1 ELSE 0 END), 0) AS matched_functions,
       COALESCE(SUM(CASE WHEN l.status='MATCH' THEN q.size ELSE 0 END), 0) AS matched_bytes,
       COALESCE(SUM(CASE WHEN l.status IS NOT NULL THEN 1 ELSE 0 END), 0) AS attempted_functions
FROM assignments x
JOIN queue q ON q.va=x.va
LEFT JOIN latest l ON l.va=x.va
WHERE x.run_id=?
"""

RUN_SUMMARY = LATEST + """
SELECT
    COALESCE(SUM(CASE WHEN f.run_id=? AND l.status='MATCH' THEN 1 ELSE 0 END), 0)
      AS new_matched_functions,
    COALESCE(SUM(CASE WHEN f.run_id=? AND l.status='MATCH' THEN q.size ELSE 0 END), 0)
      AS new_matched_bytes
FROM assignments x
JOIN queue q ON q.va=x.va
LEFT JOIN latest l ON l.va=x.va
LEFT JOIN first_match_run f ON f.va=x.va
WHERE x.run_id=?
"""

ATTEMPTS_SUMMARY = """
SELECT COUNT(*) AS attempt_count,
       COUNT(DISTINCT va) AS touched_functions,
       COALESCE(SUM(CASE WHEN status='MATCH' THEN 1 ELSE 0 END), 0) AS match_attempts
FROM attempts a LEFT JOIN attempt_reviews r ON r.attempt_id=a.id
WHERE a.run_id=? AND r.attempt_id IS NULL
"""


def one(db: sqlite3.Connection, statement: str, args=()) -> dict:
    c = db.execute(statement, args)
    row = c.fetchone()
    return dict(zip((x[0] for x in c.description), row))


def snapshot(db_path: Path, run_id: str | None) -> dict:
    if not db_path.is_file():
        raise FileNotFoundError(f'SQLite database not found: {db_path}')
    uri = 'file:' + quote(str(db_path.resolve()), safe='/') + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA busy_timeout=10000')
        db.execute('BEGIN')  # consistent read snapshot in WAL, no writes
        recovery = one(db, "SELECT COUNT(*) AS void_passes, COUNT(DISTINCT a.va) AS void_functions FROM attempt_reviews r JOIN attempts a ON a.id=r.attempt_id")
        overall = one(db, ALL_SUMMARY)
        if run_id:
            c = db.execute('SELECT run_id,workers,passes,started_utc,finished_utc '
                           'FROM runs WHERE run_id=?', (run_id,))
        else:
            c = db.execute('SELECT run_id,workers,passes,started_utc,finished_utc '
                           'FROM runs ORDER BY started_utc DESC, run_id DESC LIMIT 1')
        info = c.fetchone()
        if run_id and not info:
            raise ValueError(f'No run with ID {run_id!r} in {db_path}')
        info = dict(zip((x[0] for x in c.description), info)) if info else None
        scope = gains = attempts = None
        if info:
            rid = info['run_id']
            scope = one(db, SCOPE_SUMMARY, (rid,))
            gains = one(db, RUN_SUMMARY, (rid, rid, rid))
            attempts = one(db, ATTEMPTS_SUMMARY, (rid,))
        db.execute('COMMIT')
    return {'overall': overall, 'run': info, 'scope': scope,
            'gains': gains, 'attempts': attempts, 'recovery': recovery}


def percent(num: int, denominator: int) -> str:
    return f'{num / denominator * 100:.2f}%' if denominator else 'n/a'


def section(label: str, data: dict) -> list[str]:
    count, total = data['matched_functions'], data['total_functions']
    matched, total_bytes = data['matched_bytes'], data['total_bytes']
    return [
        label,
        f'  MATCH functions:       {count:,} / {total:,} ({percent(count, total)})',
        f'  Retail bytes matched: {matched:,} / {total_bytes:,} ({percent(matched, total_bytes)})',
        f'  Matched size:         {matched / 1024:,.2f} KiB',
        f'  Functions attempted:  {data["attempted_functions"]:,}',
    ]


def report(data: dict) -> str:
    lines = [f'Spore byte-exact progress | {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}', '']
    lines += section('ALL-TIME QUEUE', data['overall'])
    r = data.get('recovery')
    if r and r['void_passes']:
        lines += [f'  VOID_PROVIDER:        {r["void_passes"]:,} preserved but excluded passes across {r["void_functions"]:,} functions']
    info = data['run']
    if info:
        scope, gains, attempts = data['scope'], data['gains'], data['attempts']
        lines += ['', *section('SPGONZAGO (CUMULATIVE)', scope)]
        lines += ['', 'SELECTED PARALLEL RUN',
                  f'  ID:                    {info["run_id"]}',
                  f'  Started (UTC):         {info["started_utc"]}',
                  f'  State:                 {"finished" if info["finished_utc"] else "not marked finished"}',
                  f'  Configuration:         {info["workers"]} workers, {info["passes"]} passes',
                  f'  New byte-exact MATCH:  +{gains["new_matched_functions"]:,} functions',
                  f'  New retail bytes:      +{gains["new_matched_bytes"]:,} bytes '
                  f'({gains["new_matched_bytes"] / 1024:,.2f} KiB)',
                  f'  Scope gain:            +{percent(gains["new_matched_functions"], scope["total_functions"])} '
                  f'of assigned functions',
                  f'  Attempts this run:     {attempts["attempt_count"]:,} across '
                  f'{attempts["touched_functions"]:,} functions']
        lines += ['', 'Run gains count functions whose FIRST recorded MATCH occurred in',
                  'this run and whose latest result remains MATCH. Each is counted once.']
    else:
        lines += ['', 'No parallel run recorded in SQLite yet; SPGonzago scope and run gains unavailable.']
    lines += ['Read-only SQLite snapshot. Function sizes are from the retail queue.']
    return '\n'.join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description='Matched retail bytes: all-time, SPGonzago, and current parallel run')
    p.add_argument('--db', type=Path, default=DEFAULT_DB, help='Path to spore.sqlite3')
    p.add_argument('--run-id', help='Inspect a particular parallel run (default: latest)')
    p.add_argument('--watch', type=float, metavar='SECONDS', help='Refresh, e.g. --watch 10')
    args = p.parse_args()
    if args.watch is not None and args.watch <= 0:
        p.error('--watch must be positive')
    try:
        while True:
            output = report(snapshot(args.db, args.run_id))
            if args.watch:
                print('\033[2J\033[H', end='')
            print(output, flush=True)
            if args.watch is None:
                break
            time.sleep(args.watch)
    except (FileNotFoundError, ValueError, sqlite3.Error) as ex:
        print(f'Error: {ex}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
