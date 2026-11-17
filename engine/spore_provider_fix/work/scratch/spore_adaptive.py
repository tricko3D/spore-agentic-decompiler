"""Shared, token-verified progressive Sail Research concurrency.

A new slot is admitted only after a live inference reports actual output
tokens. Provider 429s roll back the unverified probe, not productive workers.
All decisions are SQLite-atomic across separate worker processes.
"""
import time
import spore_sqlite_store as store


def snapshot():
    with store.connect() as db:
        row=db.execute('SELECT * FROM adaptive_control WHERE singleton=1').fetchone()
        return dict(row) if row else None


def begin(run_id,ceiling,start=1):
    if not (1 <= start <= ceiling <= 32):
        raise ValueError('Expected 1 <= initial concurrency <= ceiling <= 32')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            db.execute('''INSERT OR REPLACE INTO adaptive_control
                          VALUES(1,?,?,?,0,0,0,0,0,?,?)''',
                       (run_id,start,ceiling,time.time(),store.utc()))
            db.execute('COMMIT')
        except Exception:
            db.execute('ROLLBACK')
            raise


def confirm_output(run_id,slot,now=None):
    """One verified stream per slot, based on positive step_finish output tokens.

    Repeated token events from one agent never open additional slots. A new
    probe starts only when every admitted inference has proven it can produce.
    """
    if not run_id or not slot:
        return None
    now=time.time() if now is None else now
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            row=db.execute('SELECT * FROM adaptive_control WHERE singleton=1').fetchone()
            owner=db.execute('SELECT run_id,verified FROM inference_slots WHERE id=?',(slot,)).fetchone()
            if not row or row['run_id']!=run_id or not owner or owner['run_id']!=run_id or owner['verified']:
                db.execute('COMMIT')
                return None
            db.execute('UPDATE inference_slots SET verified=1 WHERE id=?',(slot,))
            healthy=db.execute('SELECT COUNT(*) FROM inference_slots WHERE run_id=? AND verified=1',
                               (run_id,)).fetchone()[0]
            active=db.execute('SELECT COUNT(*) FROM inference_slots WHERE run_id=?',(run_id,)).fetchone()[0]
            cap=row['concurrency']
            next_cap=cap
            if (now >= row['cooldown_until'] and healthy==active
                    and active >= cap and cap < row['ceiling']):
                next_cap=cap+1
            db.execute('''UPDATE adaptive_control
                          SET concurrency=?,clean_streak=clean_streak+1,
                              productive_samples=productive_samples+1,
                              last_probe=?,updated_utc=? WHERE singleton=1''',
                       (next_cap,now if next_cap!=cap else row['last_probe'],store.utc()))
            db.execute('COMMIT')
            if next_cap!=cap:
                return f'verified output tokens: concurrency {cap}->{next_cap}, ceiling {row["ceiling"]}'
            return None
        except Exception:
            db.execute('ROLLBACK')
            raise


def observe(run_id,overloaded=False,productive=False,now=None):
    """Back off after confirmed provider capacity errors, not generic retries."""
    if not overloaded and not productive:
        return None
    now=time.time() if now is None else now
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            row=db.execute('SELECT * FROM adaptive_control WHERE singleton=1').fetchone()
            if not row or row['run_id']!=run_id:
                db.execute('COMMIT')
                return None
            cap=row['concurrency']
            failures=row['provider_failures']
            strikes=row['consecutive_errors']
            paused=row['cooldown_until']
            next_cap=cap
            message=None
            if overloaded:
                failures+=1
                if now >= paused:
                    strikes+=1
                    healthy=db.execute('SELECT COUNT(*) FROM inference_slots WHERE run_id=? AND verified=1',
                                       (run_id,)).fetchone()[0]
                    # Keep every verified active request running; at most one
                    # unverified probe is discarded per overload wave.
                    next_cap=max(1,min(cap-1,healthy if healthy else 1))
                    seconds=min(600,30*2**min(strikes-1,4))
                    paused=now+seconds
                    message=(f'provider pressure: concurrency {cap}->{next_cap}, '
                             f'preserved {healthy} verified active streams, cooldown {seconds}s')
            db.execute('''UPDATE adaptive_control
                          SET concurrency=?,clean_streak=CASE WHEN ? THEN 0 ELSE clean_streak END,
                              provider_failures=?,consecutive_errors=?,
                              cooldown_until=?,updated_utc=? WHERE singleton=1''',
                       (next_cap,int(overloaded),failures,strikes,paused,store.utc()))
            db.execute('COMMIT')
            return message
        except Exception:
            db.execute('ROLLBACK')
            raise
