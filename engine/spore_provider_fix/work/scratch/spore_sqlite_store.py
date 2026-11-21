"""Durable concurrent state for Spore module-scoped decompilation.

Only the queue, attempts, assignments, claims, pass limit and spending state live
in SQLite. C++ sources and JSONL logs deliberately remain ordinary repo files.
All writers use short BEGIN IMMEDIATE transactions, WAL and busy_timeout.
"""
from __future__ import annotations
import csv
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid
import time

ROOT = Path.cwd()
WORK = ROOT / 'work' / 'scratch' / 'codex-pass1'
DB = WORK / 'spore.sqlite3'
QUEUE = WORK / 'queue.csv'
RESULTS = WORK / 'results.csv'
BUDGET_JSON = WORK / 'openrouter-budget.json'
SCHEMA = 1


def utc():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


@contextmanager
def connect():
    WORK.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB, timeout=60, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=60000')
    db.execute('PRAGMA foreign_keys=ON')
    try:
        yield db
    finally:
        db.close()


def init_schema():
    with connect() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        db.executescript('''
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS queue(
          va TEXT PRIMARY KEY, position INTEGER NOT NULL UNIQUE,
          size INTEGER NOT NULL, suggested_file TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS attempts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          va TEXT NOT NULL REFERENCES queue(va),
          pass_number INTEGER NOT NULL,
          status TEXT NOT NULL, effort TEXT NOT NULL DEFAULT '',
          note TEXT NOT NULL DEFAULT '', utc TEXT NOT NULL,
          run_id TEXT, worker_idx INTEGER, log_path TEXT);
        CREATE INDEX IF NOT EXISTS attempts_va_idx ON attempts(va,id);
        CREATE TABLE IF NOT EXISTS budget(
          singleton INTEGER PRIMARY KEY CHECK(singleton=1),
          key_fingerprint TEXT NOT NULL,
          baseline_usage REAL NOT NULL,
          last_usage REAL NOT NULL,
          updated_utc TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reservations(
          reservation_id TEXT PRIMARY KEY, run_id TEXT,
          va TEXT NOT NULL, amount REAL NOT NULL CHECK(amount>0),
          created_utc TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS run_budgets(
          run_id TEXT PRIMARY KEY,
          key_fingerprint TEXT NOT NULL,
          baseline_usage REAL NOT NULL,
          last_usage REAL NOT NULL,
          budget_limit REAL NOT NULL CHECK(budget_limit>0),
          updated_utc TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs(
          run_id TEXT PRIMARY KEY, workers INTEGER NOT NULL, passes INTEGER NOT NULL,
          started_utc TEXT NOT NULL, finished_utc TEXT);
        CREATE TABLE IF NOT EXISTS assignments(
          run_id TEXT NOT NULL REFERENCES runs(run_id),
          va TEXT NOT NULL REFERENCES queue(va), worker_idx INTEGER NOT NULL,
          group_key TEXT NOT NULL DEFAULT '', PRIMARY KEY(run_id,va));
        CREATE INDEX IF NOT EXISTS assignments_worker_idx ON assignments(run_id,worker_idx);
        CREATE TABLE IF NOT EXISTS claims(
          va TEXT PRIMARY KEY REFERENCES queue(va),
          run_id TEXT NOT NULL, worker_idx INTEGER NOT NULL,
          pid INTEGER NOT NULL, started_utc TEXT NOT NULL,
          pid_start TEXT, child_pid INTEGER, child_start TEXT);
        CREATE TABLE IF NOT EXISTS attempt_reviews(
          attempt_id INTEGER PRIMARY KEY REFERENCES attempts(id),
          classification TEXT NOT NULL CHECK(classification='VOID_PROVIDER'),
          reason TEXT NOT NULL, evidence TEXT NOT NULL, reviewed_utc TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS provider_incidents(
          id INTEGER PRIMARY KEY AUTOINCREMENT, va TEXT NOT NULL REFERENCES queue(va),
          run_id TEXT, worker_idx INTEGER, pass_number INTEGER NOT NULL,
          reason TEXT NOT NULL, log_path TEXT, utc TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS agent_sessions(
          va TEXT PRIMARY KEY REFERENCES queue(va), session_id TEXT NOT NULL,
          updated_utc TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS inference_slots(
          id TEXT PRIMARY KEY, run_id TEXT, va TEXT NOT NULL,
          created_utc TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS run_exclusions(
          run_id TEXT NOT NULL REFERENCES runs(run_id),
          va TEXT NOT NULL REFERENCES queue(va),
          reason TEXT NOT NULL, created_utc TEXT NOT NULL,
          PRIMARY KEY(run_id,va));
        CREATE TABLE IF NOT EXISTS run_attempt_limits(
          run_id TEXT NOT NULL REFERENCES runs(run_id),
          va TEXT NOT NULL REFERENCES queue(va),
          max_attempts INTEGER NOT NULL,
          PRIMARY KEY(run_id,va));
        CREATE TABLE IF NOT EXISTS run_delays(
          run_id TEXT NOT NULL REFERENCES runs(run_id),
          va TEXT NOT NULL REFERENCES queue(va),
          retry_after REAL NOT NULL,
          PRIMARY KEY(run_id,va));
        CREATE TABLE IF NOT EXISTS adaptive_control(
          singleton INTEGER PRIMARY KEY CHECK(singleton=1),
          run_id TEXT NOT NULL,concurrency INTEGER NOT NULL,ceiling INTEGER NOT NULL,
          clean_streak INTEGER NOT NULL, productive_samples INTEGER NOT NULL,
          provider_failures INTEGER NOT NULL,consecutive_errors INTEGER NOT NULL,
          cooldown_until REAL NOT NULL,last_probe REAL NOT NULL,updated_utc TEXT NOT NULL);
        ''')
        # Additive, idempotent migration for databases created before work stealing.
        for table, columns in {
            'claims': {'pid_start': 'TEXT', 'child_pid': 'INTEGER', 'child_start': 'TEXT'},
            'assignments': {'group_key': "TEXT NOT NULL DEFAULT ''"},
            'inference_slots': {'verified': 'INTEGER NOT NULL DEFAULT 0'},
        }.items():
            found = {r['name'] for r in db.execute(f'PRAGMA table_info({table})')}
            for name, kind in columns.items():
                if name not in found:
                    db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {kind}')


def has_import():
    with connect() as db:
        return db.execute("SELECT 1 FROM metadata WHERE key='legacy_import'").fetchone() is not None


def assert_legacy_unchanged():
    """Refuse to start workers if the old CSV runner wrote new history after cutover."""
    with connect() as db:
        row=db.execute("SELECT value FROM metadata WHERE key='legacy_import'").fetchone()
        if row is None:
            raise RuntimeError('SQLite migration missing')
        old=json.loads(row['value'])['fingerprints']
    now=legacy_fingerprints()
    if old != now:
        raise RuntimeError('Legacy CSV or budget JSON changed after SQLite migration. '
                           'Stop the legacy runner and reconcile the new rows before parallel execution.')


def ensure_imported():
    if not DB.exists():
        raise RuntimeError('SQLite migration not completed. Run: .venv/bin/python migrate_spore_sqlite.py')
    # CREATE IF NOT EXISTS only: preserve every historical attempt and CSV fingerprint.
    init_schema()
    with connect() as db:
        if not db.execute("SELECT 1 FROM metadata WHERE key='legacy_import'").fetchone():
            raise RuntimeError('SQLite migration not completed. Run: .venv/bin/python migrate_spore_sqlite.py')
        if not db.execute('SELECT 1 FROM queue LIMIT 1').fetchone():
            raise RuntimeError('SQLite queue is empty; migration appears incomplete.')


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def legacy_fingerprints():
    return {p.name: _sha(p) if p.exists() else None for p in (QUEUE, RESULTS, BUDGET_JSON)}


def migrate_legacy():
    """Idempotent, lossless, one-time CSV/JSON import. Leaves originals intact."""
    init_schema()
    if not QUEUE.is_file() or not RESULTS.is_file():
        raise RuntimeError('Missing queue.csv or results.csv. Do NOT run init on an existing queue.')
    stamp_before = legacy_fingerprints()
    with QUEUE.open(newline='', encoding='utf-8') as f:
        queue = list(csv.DictReader(f))
    with RESULTS.open(newline='', encoding='utf-8') as f:
        results = list(csv.DictReader(f))
    budget = json.loads(BUDGET_JSON.read_text()) if BUDGET_JSON.exists() else None
    keys = set()
    prepared = []
    for index, row in enumerate(queue, 1):
        va = f'0x{int(row["va"],16):08x}'
        if va in keys:
            raise RuntimeError(f'Duplicate queue VA: {va}')
        keys.add(va)
        prepared.append((va,index,int(row['size']),row['suggested_file']))
    counts = {}
    attempts = []
    for row in results:
        va = f'0x{int(row["va"],16):08x}'
        if va not in keys:
            raise RuntimeError(f'Historical result VA not in queue: {va}')
        status = row['status']
        if status not in ('MATCH','FAIL','DEFER','SKIP','EQUIV'):
            raise RuntimeError(f'Unrecognized historical status {status!r} at {va}')
        next_pass = counts.get(va,0)+1
        attempts.append((va,next_pass,status,row.get('effort',''),row.get('note',''),row.get('utc','')))
        if status in ('FAIL','DEFER'):
            counts[va]=next_pass
    stamp_after = legacy_fingerprints()
    if stamp_after != stamp_before:
        raise RuntimeError('Legacy files changed during migration. Stop the CSV runner first and retry.')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            was = db.execute("SELECT value FROM metadata WHERE key='legacy_import'").fetchone()
            if was:
                old = json.loads(was['value'])
                if old['fingerprints'] != stamp_before:
                    raise RuntimeError('CSV/JSON changed since previous migration. Do not run old runner after cutover.')
                db.execute('COMMIT')
                return {'already':True,'queue':len(prepared),'attempts':len(attempts)}
            if db.execute('SELECT 1 FROM attempts LIMIT 1').fetchone() or db.execute('SELECT 1 FROM queue LIMIT 1').fetchone():
                raise RuntimeError('Database already contains queue/attempts without migration marker. Refusing to overwrite.')
            db.executemany('INSERT INTO queue(va,position,size,suggested_file) VALUES(?,?,?,?)',prepared)
            db.executemany('INSERT INTO attempts(va,pass_number,status,effort,note,utc) VALUES(?,?,?,?,?,?)',attempts)
            if budget:
                db.execute('INSERT INTO budget(singleton,key_fingerprint,baseline_usage,last_usage,updated_utc) VALUES(1,?,?,?,?)',
                  (budget['key_fingerprint'],float(budget['baseline_usage']),float(budget['last_usage']),budget.get('updated_utc','')))
            db.execute('INSERT INTO metadata(key,value) VALUES(?,?)',('legacy_import',json.dumps({'schema':SCHEMA,'fingerprints':stamp_before,'utc':utc()})))
            db.execute('INSERT INTO metadata(key,value) VALUES(?,?)',('pass_limit','3'))
            db.execute('COMMIT')
        except Exception:
            db.execute('ROLLBACK')
            raise
    return {'already':False,'queue':len(prepared),'attempts':len(attempts),'budget':bool(budget)}


def read_queue():
    ensure_imported()
    with connect() as db:
        return [dict(r) for r in db.execute('SELECT va,size,suggested_file FROM queue ORDER BY position')]


def read_attempts():
    ensure_imported()
    with connect() as db:
        return [dict(r) for r in db.execute('''SELECT a.va,a.status,a.effort,a.note,a.utc,a.pass_number,a.id,a.log_path,a.run_id
                FROM attempts a LEFT JOIN attempt_reviews v ON v.attempt_id=a.id
                WHERE v.attempt_id IS NULL ORDER BY a.id''')]


def latest():
    with connect() as db:
        rows=db.execute('''SELECT a.va,a.status,a.effort,a.note,a.utc,a.pass_number
            FROM attempts a JOIN (
                 SELECT a2.va,MAX(a2.id) id FROM attempts a2
                 LEFT JOIN attempt_reviews v ON v.attempt_id=a2.id
                 WHERE v.attempt_id IS NULL GROUP BY a2.va
            ) b ON a.id=b.id''')
        return {r['va']:dict(r) for r in rows}


def record(va,status,effort='',note='',run_id=None,worker_idx=None,log_path=None):
    if status not in ('MATCH','FAIL','DEFER','SKIP','EQUIV'):
        raise ValueError('Invalid result')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            if run_id is not None:
                claim=db.execute('SELECT run_id,worker_idx FROM claims WHERE va=?',(va,)).fetchone()
                if not claim or (claim['run_id'],claim['worker_idx'])!=(run_id,worker_idx):
                    raise RuntimeError(f'No exclusive claim for {va} owned by this worker')
            count=db.execute("""SELECT COUNT(*) FROM attempts a LEFT JOIN attempt_reviews v ON v.attempt_id=a.id
                WHERE a.va=? AND a.status IN ('FAIL','DEFER') AND v.attempt_id IS NULL""",(va,)).fetchone()[0]
            db.execute('''INSERT INTO attempts(va,pass_number,status,effort,note,utc,run_id,worker_idx,log_path)
                          VALUES(?,?,?,?,?,?,?,?,?)''',
                          (va,count+1,status,effort,note,utc(),run_id,worker_idx,str(log_path) if log_path else None))
            db.execute('COMMIT')
        except Exception:
            db.execute('ROLLBACK')
            raise


def reset(va):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            if db.execute('SELECT 1 FROM claims WHERE va=?',(va,)).fetchone():
                raise RuntimeError(f'{va} is in progress; cannot reset it.')
            db.execute('DELETE FROM attempt_reviews WHERE attempt_id IN (SELECT id FROM attempts WHERE va=?)',(va,))
            n=db.execute('DELETE FROM attempts WHERE va=?',(va,)).rowcount
            db.execute('COMMIT')
            return n
        except Exception:
            db.execute('ROLLBACK')
            raise


def set_pass_limit(n):
    with connect() as db:
        db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('pass_limit',?)",(str(n),))


def pass_limit():
    with connect() as db:
        r=db.execute("SELECT value FROM metadata WHERE key='pass_limit'").fetchone()
        return int(r['value']) if r else 3


def create_run(run_id,workers,passes,assignments,group_keys=None):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            db.execute('INSERT INTO runs(run_id,workers,passes,started_utc) VALUES(?,?,?,?)',
                       (run_id,workers,passes,utc()))
            db.executemany('INSERT INTO assignments(run_id,va,worker_idx,group_key) VALUES(?,?,?,?)',
                           ((run_id,va,i,(group_keys or {}).get(va,va)) for va,i in assignments.items()))
            # Snapshot valid FAIL/DEFER counts before workers start. Every assignment
            # receives exactly 'passes' new attempts even after prior exhausted runs.
            db.execute("""
                INSERT INTO run_attempt_limits(run_id,va,max_attempts)
                SELECT ?,a.va,? + (
                  SELECT COUNT(*) FROM attempts t
                  LEFT JOIN attempt_reviews r ON r.attempt_id=t.id
                  WHERE t.va=a.va AND t.status IN ('FAIL','DEFER')
                    AND r.attempt_id IS NULL
                )
                FROM assignments a WHERE a.run_id=?
            """, (run_id,passes,run_id))
            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('pass_limit',?)",(str(passes),))
            db.execute('COMMIT')
        except Exception:
            db.execute('ROLLBACK')
            raise


def run_attempt_limit(run_id, va):
    """Cumulative ceiling captured when this run was created; never reset history."""
    with connect() as db:
        row = db.execute(
            'SELECT max_attempts FROM run_attempt_limits WHERE run_id=? AND va=?',
            (run_id, va)
        ).fetchone()
        if row is None:
            raise RuntimeError(f'Missing run attempt ceiling for {run_id} {va}')
        return int(row['max_attempts'])


def finish_run(run_id):
    with connect() as db:
        db.execute('UPDATE runs SET finished_utc=? WHERE run_id=?',(utc(),run_id))


def assigned(va,run_id,worker_idx):
    if not run_id:
        return True
    with connect() as db:
        return db.execute('SELECT 1 FROM assignments WHERE run_id=? AND va=? AND worker_idx=?',
                          (run_id,va,worker_idx)).fetchone() is not None


def claim(va,run_id,worker_idx,passes):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            if run_id and not db.execute('SELECT 1 FROM assignments WHERE run_id=? AND va=? AND worker_idx=?',
                          (run_id,va,worker_idx)).fetchone():
                db.execute('COMMIT'); return False
            if db.execute('SELECT 1 FROM claims WHERE va=?',(va,)).fetchone():
                db.execute('COMMIT'); return False
            latest=db.execute('''SELECT a.status FROM attempts a LEFT JOIN attempt_reviews v ON v.attempt_id=a.id
                WHERE a.va=? AND v.attempt_id IS NULL ORDER BY a.id DESC LIMIT 1''',(va,)).fetchone()
            n=db.execute("""SELECT COUNT(*) FROM attempts a LEFT JOIN attempt_reviews v ON v.attempt_id=a.id
                WHERE a.va=? AND a.status IN ('FAIL','DEFER') AND v.attempt_id IS NULL""",(va,)).fetchone()[0]
            if (latest and latest['status'] in ('MATCH','SKIP','EQUIV')) or n>=passes:
                db.execute('COMMIT'); return False
            db.execute('INSERT INTO claims(va,run_id,worker_idx,pid,started_utc) VALUES(?,?,?,?,?)',
                       (va,run_id or 'serial',worker_idx,os.getpid(),utc()))
            db.execute('COMMIT'); return True
        except Exception:
            db.execute('ROLLBACK'); raise


def pid_identity(pid):
    """Linux /proc start tick, resilient to ordinary PID reuse; None for zombies."""
    if not pid or pid <= 0:
        return None
    try:
        data = (Path('/proc')/str(pid)/'stat').read_text()
        tail = data.rsplit(')', 1)[1].split()
        if tail[0] in ('Z','X'):
            return None
        return tail[19]
    except (OSError, IndexError, ValueError):
        return None


def record_child(va, child_pid):
    """Record OpenCode's independent process group so a crashed worker is safe to reclaim."""
    with connect() as db:
        db.execute("""UPDATE claims SET child_pid=?, child_start=?
                      WHERE va=? AND pid=?""",
                   (child_pid, pid_identity(child_pid), va, os.getpid()))


def _cleanup_dead_claim(db, row):
    """Clean up a dead worker and its known child before releasing ownership."""
    worker_start = pid_identity(row['pid'])
    if worker_start and (not row['pid_start'] or worker_start == row['pid_start']):
        return False  # Live owners are NEVER reclaimed due to elapsed time.
    child = row['child_pid']
    if child and row['child_start'] and pid_identity(child) == row['child_start']:
        import signal
        import time
        try:
            os.killpg(child, signal.SIGTERM)
        except ProcessLookupError:
            pass
        for _ in range(10):
            if pid_identity(child) != row['child_start']:
                break
            time.sleep(.1)
        if pid_identity(child) == row['child_start']:
            try:
                os.killpg(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
        # Never reassign while a known child is still alive.
        if pid_identity(child) == row['child_start']:
            return False
    db.execute('DELETE FROM claims WHERE va=? AND pid=?', (row['va'], row['pid']))
    return True


def claim_next(run_id, worker_idx, passes):
    """Atomic work stealing: (VA, busy) where busy means another live worker owns a claim.

    Assignments form the run's target *set*, not fixed worker ownership. Only
    one worker may work in a shared source group at a time.
    """
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            for row in db.execute('SELECT * FROM claims WHERE run_id=?', (run_id,)).fetchall():
                _cleanup_dead_claim(db, row)
            chosen = db.execute("""
                SELECT q.va FROM assignments a
                JOIN queue q ON q.va=a.va
                JOIN run_attempt_limits cap ON cap.run_id=a.run_id AND cap.va=a.va
                LEFT JOIN claims held ON held.va=q.va
                LEFT JOIN run_exclusions excluded ON excluded.va=q.va AND excluded.run_id=a.run_id
                LEFT JOIN run_delays delayed ON delayed.va=q.va AND delayed.run_id=a.run_id
                LEFT JOIN attempts last ON last.id=(
                    SELECT MAX(t.id) FROM attempts t
                    LEFT JOIN attempt_reviews r ON r.attempt_id=t.id
                    WHERE t.va=q.va AND r.attempt_id IS NULL)
                WHERE a.run_id=? AND held.va IS NULL AND excluded.va IS NULL
                  AND (delayed.retry_after IS NULL OR delayed.retry_after <= ?)
                  AND (last.status IS NULL OR last.status NOT IN ('MATCH','SKIP','EQUIV'))
                  AND (SELECT COUNT(*) FROM attempts t
                       LEFT JOIN attempt_reviews r ON r.attempt_id=t.id
                       WHERE t.va=q.va AND t.status IN ('FAIL','DEFER')
                         AND r.attempt_id IS NULL) < cap.max_attempts
                  AND NOT EXISTS(
                       SELECT 1 FROM claims locked
                       JOIN assignments same ON same.va=locked.va AND same.run_id=a.run_id
                       WHERE same.group_key=a.group_key)
                ORDER BY q.position LIMIT 1
            """, (run_id, time.time())).fetchone()
            if chosen:
                va = chosen['va']
                db.execute("""INSERT INTO claims
                    (va,run_id,worker_idx,pid,started_utc,pid_start)
                    VALUES(?,?,?,?,?,?)""",
                    (va,run_id,worker_idx,os.getpid(),utc(),pid_identity(os.getpid())))
                db.execute('COMMIT')
                return va, True
            busy = db.execute('SELECT 1 FROM claims WHERE run_id=? LIMIT 1',(run_id,)).fetchone() is not None
            if not busy:
                # Keep work-stealing workers alive until postponed provider
                # probes are eligible again, without losing their function.
                busy = db.execute('SELECT 1 FROM run_delays WHERE run_id=? AND retry_after>? LIMIT 1',
                                  (run_id,time.time())).fetchone() is not None
            db.execute('COMMIT')
            return None, busy
        except Exception:
            db.execute('ROLLBACK')
            raise


def defer_run_target(run_id,va,seconds):
    """Release an uncounted provider failure to the shared queue after backoff."""
    with connect() as db:
        db.execute('''INSERT INTO run_delays(run_id,va,retry_after) VALUES(?,?,?)
                      ON CONFLICT(run_id,va) DO UPDATE SET retry_after=excluded.retry_after''',
                   (run_id,va,time.time()+max(1,seconds)))


def ignore_run_target(run_id, va, reason):
    with connect() as db:
        db.execute('''INSERT OR IGNORE INTO run_exclusions
                      (run_id,va,reason,created_utc) VALUES(?,?,?,?)''',
                   (run_id,va,reason,utc()))


def history_for(va):
    with connect() as db:
        return [dict(row) for row in db.execute("""SELECT a.va,a.status,a.effort,a.note,a.utc,a.pass_number,a.id,a.log_path,a.run_id
                       FROM attempts a LEFT JOIN attempt_reviews v ON v.attempt_id=a.id
                       WHERE a.va=? AND v.attempt_id IS NULL ORDER BY a.id""", (va,))]


def release_claim(va,run_id,worker_idx):
    with connect() as db:
        db.execute('DELETE FROM claims WHERE va=? AND run_id=? AND worker_idx=?',
                   (va,run_id or 'serial',worker_idx))


def clear_stale_claims():
    # Coordinator startup: never blindly delete a claim while an agent may be alive.
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            rows = db.execute('SELECT * FROM claims').fetchall()
            cleared = sum(_cleanup_dead_claim(db, row) for row in rows)
            if db.execute('SELECT 1 FROM claims LIMIT 1').fetchone():
                raise RuntimeError('Active or uncertain claim owner remains. Refusing to start a new run.')
            db.execute('COMMIT')
            return cleared
        except Exception:
            db.execute('ROLLBACK')
            raise


def claimed():
    with connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM claims ORDER BY started_utc')]


def mark_provider_only(rows):
    """Audit annotation; NEVER deletes or overwrites the original attempt."""
    if not rows:
        return 0
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            if db.execute('SELECT 1 FROM claims LIMIT 1').fetchone():
                raise RuntimeError('Active function claims exist: stop all workers before recovery')
            for item in rows:
                db.execute('''INSERT OR IGNORE INTO attempt_reviews(attempt_id,classification,reason,evidence,reviewed_utc)
                    VALUES(?, 'VOID_PROVIDER', ?, ?, ?)''',
                    (item['id'],item['reason'],item['evidence'],utc()))
            db.execute('COMMIT')
            return len(rows)  # caller shows qualified candidates; INSERT OR IGNORE is idempotent
        except Exception:
            db.execute('ROLLBACK')
            raise


def save_session(va,session_id):
    if not session_id or not session_id.startswith('ses_'):
        return
    with connect() as db:
        db.execute('''INSERT INTO agent_sessions(va,session_id,updated_utc) VALUES(?,?,?)
          ON CONFLICT(va) DO UPDATE SET session_id=excluded.session_id,updated_utc=excluded.updated_utc''',
          (va,session_id,utc()))


def session_for(va):
    with connect() as db:
        r=db.execute('SELECT session_id FROM agent_sessions WHERE va=?',(va,)).fetchone()
        return r['session_id'] if r else None


def record_incident(va,reason,pass_number,log_path=None,run_id=None,worker_idx=None):
    with connect() as db:
        db.execute('''INSERT INTO provider_incidents(va,run_id,worker_idx,pass_number,reason,log_path,utc)
          VALUES(?,?,?,?,?,?,?)''',
          (va,run_id,worker_idx,pass_number,reason,str(log_path) if log_path else None,utc()))


def incident_summary():
    with connect() as db:
        invalid=db.execute('SELECT COUNT(*),COUNT(DISTINCT a.va) FROM attempt_reviews v JOIN attempts a ON a.id=v.attempt_id').fetchone()
        incidents=db.execute('SELECT COUNT(*),COUNT(DISTINCT va) FROM provider_incidents').fetchone()
        return {'invalid_passes':invalid[0], 'invalid_functions':invalid[1],
                'provider_incidents':incidents[0], 'incident_functions':incidents[1]}


def reserve_inference_slot(va,run_id,max_inflight):
    """Nonblocking, SQLite-atomic cap across local OpenRouter clients."""
    if not max_inflight:
        return None
    token=uuid.uuid4().hex
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            n=db.execute('SELECT COUNT(*) FROM inference_slots').fetchone()[0]
            effective = max_inflight
            if os.environ.get('SPORE_ADAPTIVE_INFLIGHT') == '1':
                window = db.execute('SELECT * FROM adaptive_control WHERE singleton=1').fetchone()
                if not window or window['run_id'] != run_id:
                    db.execute('COMMIT'); return None
                if time.time() < window['cooldown_until']:
                    db.execute('COMMIT'); return None
                effective = min(effective, window['concurrency'])
            if n>=effective and os.environ.get('SPORE_ADAPTIVE_INFLIGHT') == '1':
                # Re-probe once after cooldown, without terminating verified
                # active agents. No speculative bursts into an unverified slot.
                healthy=db.execute('SELECT COUNT(*) FROM inference_slots WHERE run_id=? AND verified=1',
                                   (run_id,)).fetchone()[0]
                if (window and n == effective == healthy and n > 0
                        and effective < window['ceiling']
                        and time.time() >= window['cooldown_until']
                        and time.time() - window['last_probe'] >= 5):
                    effective += 1
                    db.execute('UPDATE adaptive_control SET concurrency=?,last_probe=?,updated_utc=? WHERE singleton=1',
                               (effective,time.time(),utc()))
            if n>=effective:
                db.execute('COMMIT'); return None
            db.execute('INSERT INTO inference_slots(id,run_id,va,created_utc,verified) VALUES(?,?,?,?,0)',
                       (token,run_id,va,utc()))
            db.execute('COMMIT'); return token
        except Exception:
            db.execute('ROLLBACK');raise


def release_inference_slot(token):
    if token:
        with connect() as db:
            db.execute('DELETE FROM inference_slots WHERE id=?',(token,))


def clear_stale_inference_slots():
    with connect() as db:
        return db.execute('DELETE FROM inference_slots').rowcount


def budget_status():
    with connect() as db:
        b=db.execute('SELECT * FROM budget WHERE singleton=1').fetchone()
        reservations=db.execute('SELECT COALESCE(SUM(amount),0) FROM reservations').fetchone()[0]
        return (dict(b) if b else None,float(reservations))


def update_budget(key_fingerprint,usage,budget,limit_remaining=None,*,run_id):
    """Refresh actual /key usage and enforce the NEW RUN'S spending allowance.

    The existing budget table remains untouched as lifetime project accounting.
    This run's first successful /key reading is its own baseline, shared by all
    workers and never reset during the run, even after a worker restarts.
    We count other spend on the same OpenRouter key conservatively.
    """
    if not run_id:
        raise ValueError('Missing budget run/session ID')
    if budget<=0 or usage<0:
        raise ValueError('Invalid OpenRouter budget or usage')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            previous=db.execute('SELECT * FROM budget WHERE singleton=1').fetchone()
            if previous:
                if previous['key_fingerprint']!=key_fingerprint:
                    raise RuntimeError('Different OpenRouter API key from migrated budget state.')
                last=max(float(previous['last_usage']),float(usage))
                db.execute('UPDATE budget SET last_usage=?,updated_utc=? WHERE singleton=1',
                           (last,utc()))
            else:
                db.execute('INSERT INTO budget VALUES(1,?,?,?,?)',
                           (key_fingerprint,float(usage),float(usage),utc()))

            session=db.execute('SELECT * FROM run_budgets WHERE run_id=?',(run_id,)).fetchone()
            if session:
                if session['key_fingerprint']!=key_fingerprint:
                    raise RuntimeError('API key changed within a running decompiler session.')
                if abs(float(session['budget_limit'])-float(budget))>0.000001:
                    raise RuntimeError('Budget changed inside an active session. Start a new run.')
                baseline=float(session['baseline_usage'])
                session_last=max(float(session['last_usage']),float(usage))
                db.execute('UPDATE run_budgets SET last_usage=?,updated_utc=? WHERE run_id=?',
                           (session_last,utc(),run_id))
            else:
                # BEGIN IMMEDIATE guarantees that concurrent workers share a
                # single baseline. Never reuse the original key baseline.
                baseline=float(usage)
                session_last=baseline
                db.execute('''INSERT INTO run_budgets
                    (run_id,key_fingerprint,baseline_usage,last_usage,budget_limit,updated_utc)
                    VALUES(?,?,?,?,?,?)''',
                    (run_id,key_fingerprint,baseline,session_last,float(budget),utc()))

            reserved=float(db.execute(
                'SELECT COALESCE(SUM(amount),0) FROM reservations WHERE run_id=?',
                (run_id,)).fetchone()[0])
            all_reserved=float(db.execute(
                'SELECT COALESCE(SUM(amount),0) FROM reservations').fetchone()[0])
            spent=max(0.0,session_last-baseline)
            # Each upcoming inference requires a $0.10 conservative reserve.
            # Still respect the REAL OpenRouter key allowance independently.
            minimum_reserve=0.10
            key_ok=(limit_remaining is None or
                    float(limit_remaining) + 0.0000001 >= all_reserved+minimum_reserve)
            allowed=(spent+reserved+minimum_reserve<=float(budget)+0.0000001 and key_ok)
            db.execute('COMMIT')
            return allowed,spent,reserved
        except Exception:
            db.execute('ROLLBACK')
            raise


def update_secondary_budget(key_fingerprint, usage, budget, *, run_id):
    """Separate direct-provider baseline; never modifies OpenRouter's key history.

    This guard uses DeepInfra's official account usage, not OpenCode's
    estimated per-step costs. It is a soft run limit, not an atomic provider
    billing limit; the provider's own account spending cap remains essential.
    """
    if not run_id or budget <= 0 or usage < 0:
        raise ValueError('Invalid provider budget inputs')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            session=db.execute('SELECT * FROM run_budgets WHERE run_id=?',(run_id,)).fetchone()
            if session:
                if session['key_fingerprint']!=key_fingerprint:
                    raise RuntimeError('DeepInfra key changed during this run')
                if abs(float(session['budget_limit'])-float(budget))>0.000001:
                    raise RuntimeError('Run budget changed; start a new run')
                baseline=float(session['baseline_usage'])
                last=max(float(session['last_usage']),float(usage))
                db.execute('UPDATE run_budgets SET last_usage=?,updated_utc=? WHERE run_id=?',
                           (last,utc(),run_id))
            else:
                baseline=last=float(usage)
                db.execute('''INSERT INTO run_budgets
                    (run_id,key_fingerprint,baseline_usage,last_usage,budget_limit,updated_utc)
                    VALUES(?,?,?,?,?,?)''',
                    (run_id,key_fingerprint,baseline,last,float(budget),utc()))
            reserved=float(db.execute(
                'SELECT COALESCE(SUM(amount),0) FROM reservations WHERE run_id=?',
                (run_id,)).fetchone()[0])
            spent=max(0.0,last-baseline)
            allowed=spent+reserved+0.10<=float(budget)+0.0000001
            db.execute('COMMIT')
            return allowed,spent,reserved
        except Exception:
            db.execute('ROLLBACK')
            raise


def reserve_budget(run_id,va,amount,budget):
    """Reserve provider spend against this run, never the original key baseline."""
    if not run_id:
        raise ValueError('Budget reservation requires a run/session ID')
    token=uuid.uuid4().hex
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            session=db.execute('SELECT * FROM run_budgets WHERE run_id=?',
                               (run_id,)).fetchone()
            if not session:
                raise RuntimeError('Run budget not initialized; verify OpenRouter /key first.')
            if abs(float(session['budget_limit'])-float(budget))>0.000001:
                raise RuntimeError('Run budget mismatch between workers.')
            reserved=float(db.execute(
                'SELECT COALESCE(SUM(amount),0) FROM reservations WHERE run_id=?',
                (run_id,)).fetchone()[0])
            spent=max(0.0,float(session['last_usage'])-float(session['baseline_usage']))
            if spent+reserved+float(amount)>float(session['budget_limit'])+0.0000001:
                db.execute('COMMIT')
                return None
            db.execute('INSERT INTO reservations(reservation_id,run_id,va,amount,created_utc) VALUES(?,?,?,?,?)',
                       (token,run_id,va,amount,utc()))
            db.execute('COMMIT')
            return token
        except Exception:
            db.execute('ROLLBACK')
            raise


def release_reservation(token):
    if token:
        with connect() as db:
            db.execute('DELETE FROM reservations WHERE reservation_id=?',(token,))


def clear_stale_reservations():
    # Called only after coordinator verifies there are no active SQLite workers.
    with connect() as db:
        return db.execute('DELETE FROM reservations').rowcount


def summary():
    with connect() as db:
        q=db.execute('SELECT COUNT(*) FROM queue').fetchone()[0]
        rows=db.execute('''SELECT a.va,a.status,a.effort FROM attempts a JOIN
          (SELECT a2.va,MAX(a2.id) id FROM attempts a2 LEFT JOIN attempt_reviews v ON v.attempt_id=a2.id
           WHERE v.attempt_id IS NULL GROUP BY a2.va) b ON a.id=b.id''').fetchall()
        n=db.execute('SELECT COUNT(*) FROM attempts a LEFT JOIN attempt_reviews v ON v.attempt_id=a.id WHERE v.attempt_id IS NULL').fetchone()[0]
        return q,rows,n
