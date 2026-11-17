"""Durable concurrent state for SPGonzago decompilation.

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
        CREATE TABLE IF NOT EXISTS runs(
          run_id TEXT PRIMARY KEY, workers INTEGER NOT NULL, passes INTEGER NOT NULL,
          started_utc TEXT NOT NULL, finished_utc TEXT);
        CREATE TABLE IF NOT EXISTS assignments(
          run_id TEXT NOT NULL REFERENCES runs(run_id),
          va TEXT NOT NULL REFERENCES queue(va), worker_idx INTEGER NOT NULL,
          PRIMARY KEY(run_id,va));
        CREATE INDEX IF NOT EXISTS assignments_worker_idx ON assignments(run_id,worker_idx);
        CREATE TABLE IF NOT EXISTS claims(
          va TEXT PRIMARY KEY REFERENCES queue(va),
          run_id TEXT NOT NULL, worker_idx INTEGER NOT NULL,
          pid INTEGER NOT NULL, started_utc TEXT NOT NULL);
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
        ''')


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
        if status not in ('MATCH','FAIL','DEFER','SKIP'):
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
    if status not in ('MATCH','FAIL','DEFER','SKIP'):
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


def create_run(run_id,workers,passes,assignments):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            db.execute('INSERT INTO runs(run_id,workers,passes,started_utc) VALUES(?,?,?,?)',
                       (run_id,workers,passes,utc()))
            db.executemany('INSERT INTO assignments(run_id,va,worker_idx) VALUES(?,?,?)',
                           ((run_id,va,i) for va,i in assignments.items()))
            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('pass_limit',?)",(str(passes),))
            db.execute('COMMIT')
        except Exception:
            db.execute('ROLLBACK')
            raise


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
            if (latest and latest['status'] in ('MATCH','SKIP')) or n>=passes:
                db.execute('COMMIT'); return False
            db.execute('INSERT INTO claims(va,run_id,worker_idx,pid,started_utc) VALUES(?,?,?,?,?)',
                       (va,run_id or 'serial',worker_idx,os.getpid(),utc()))
            db.execute('COMMIT'); return True
        except Exception:
            db.execute('ROLLBACK'); raise


def release_claim(va,run_id,worker_idx):
    with connect() as db:
        db.execute('DELETE FROM claims WHERE va=? AND run_id=? AND worker_idx=?',
                   (va,run_id or 'serial',worker_idx))


def clear_stale_claims():
    # Called by coordinator ONLY when no previous SQLite runner processes exist.
    with connect() as db:
        return db.execute('DELETE FROM claims').rowcount


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
            if n>=max_inflight:
                db.execute('COMMIT'); return None
            db.execute('INSERT INTO inference_slots VALUES(?,?,?,?)',(token,run_id,va,utc()))
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


def update_budget(key_fingerprint,usage,budget,limit_remaining=None):
    """Compare observed actual key usage against preserved baseline under a write lock."""
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            b=db.execute('SELECT * FROM budget WHERE singleton=1').fetchone()
            if b:
                if b['key_fingerprint']!=key_fingerprint:
                    raise RuntimeError('Different OpenRouter API key from migrated budget state.')
                baseline=float(b['baseline_usage'])
                last=max(float(b['last_usage']),float(usage))
                db.execute('UPDATE budget SET last_usage=?,updated_utc=? WHERE singleton=1',(last,utc()))
            else:
                baseline=float(usage);last=baseline
                db.execute('INSERT INTO budget VALUES(1,?,?,?,?)',(key_fingerprint,baseline,last,utc()))
            reserved=float(db.execute('SELECT COALESCE(SUM(amount),0) FROM reservations').fetchone()[0])
            spent=last-baseline
            allowed=(spent+reserved<budget and (limit_remaining is None or float(limit_remaining)>reserved))
            db.execute('COMMIT')
            return allowed,spent,reserved
        except Exception:
            db.execute('ROLLBACK');raise


def reserve_budget(run_id,va,amount,budget):
    """Conservative per-inference reservation, prevents workers oversubscribing local budget."""
    token=uuid.uuid4().hex
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            b=db.execute('SELECT baseline_usage,last_usage FROM budget WHERE singleton=1').fetchone()
            if not b:
                raise RuntimeError('Budget state not initialized')
            reserved=float(db.execute('SELECT COALESCE(SUM(amount),0) FROM reservations').fetchone()[0])
            if float(b['last_usage'])-float(b['baseline_usage'])+reserved+amount>budget:
                db.execute('COMMIT');return None
            db.execute('INSERT INTO reservations(reservation_id,run_id,va,amount,created_utc) VALUES(?,?,?,?,?)',
                       (token,run_id,va,amount,utc()))
            db.execute('COMMIT');return token
        except Exception:
            db.execute('ROLLBACK');raise


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
