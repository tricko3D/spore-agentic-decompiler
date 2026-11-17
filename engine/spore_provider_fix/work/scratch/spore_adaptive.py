"""Sail Research adaptive congestion control, cooperative across SQLite workers.

Observations are full OpenCode pass outcomes, not individual underlying requests.
One shared cap, an AIMD increase/decrease controller, and exponential cooldown.
"""
from datetime import datetime, timezone
import time
import spore_sqlite_store as store


def snapshot():
    with store.connect() as db:
        row = db.execute('SELECT * FROM adaptive_control WHERE singleton=1').fetchone()
        return dict(row) if row else None


def begin(run_id, ceiling, start=1):
    if not (1 <= start <= ceiling <= 32):
        raise ValueError('Expected 1 <= initial concurrency <= ceiling <= 32')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            db.execute('''INSERT OR REPLACE INTO adaptive_control
                          VALUES(1,?,?,?,0,0,0,0,0,?,?)''',
                       (run_id, start, ceiling, time.time(), store.utc()))
            db.execute('COMMIT')
        except Exception:
            db.execute('ROLLBACK')
            raise


def observe(run_id, overloaded=False, productive=False, now=None):
    """Record one finished pass; return a change-of-capacity message, if any."""
    if not overloaded and not productive:
        return None
    now = time.time() if now is None else now
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            row = db.execute('SELECT * FROM adaptive_control WHERE singleton=1').fetchone()
            if not row or row['run_id'] != run_id:
                db.execute('COMMIT')
                return None
            cap = row['concurrency']
            streak = row['clean_streak']
            good = row['productive_samples']
            errors = row['provider_failures']
            consecutive = row['consecutive_errors']
            paused = row['cooldown_until']
            last_probed = row['last_probe']
            message = None
            if overloaded:
                errors += 1
                streak = 0
                # Overlapping agents may all fail in the same overload wave.
                # Halve only once per cooldown epoch, not once per worker.
                if now >= paused:
                    consecutive += 1
                    reduced = max(1, cap // 2)
                    seconds = min(900, 60 * 2 ** min(consecutive - 1, 4))
                    paused = now + seconds
                    message = f'provider pressure: concurrency {cap}->{reduced}, quiet {seconds}s, strike {consecutive}'
                    cap = reduced
            elif productive:
                good += 1
                consecutive = 0
                streak += 1
                # Increase one slot after repeated clean passes and minimum probe spacing.
                if (cap < row['ceiling'] and now >= paused and
                    streak >= max(2, 2 * cap) and now - last_probed >= 90):
                    old = cap
                    cap += 1
                    streak = 0
                    last_probed = now
                    message = f'healthy passes: concurrency {old}->{cap}, ceiling {row["ceiling"]}'
            db.execute('''UPDATE adaptive_control SET concurrency=?,clean_streak=?,
                          productive_samples=?,provider_failures=?,consecutive_errors=?,
                          cooldown_until=?,last_probe=?,updated_utc=? WHERE singleton=1''',
                       (cap, streak, good, errors, consecutive, paused, last_probed, store.utc()))
            db.execute('COMMIT')
            return message
        except Exception:
            db.execute('ROLLBACK')
            raise
