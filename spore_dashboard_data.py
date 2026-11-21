"""Read-only telemetry for the local Spore dashboard. Standard library only.

The OpenRouter charged total and OpenCode step_finish costs are different
accounting sources. Never invent per-attempt charges when a step is missing.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from functools import lru_cache
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re
import sqlite3
import threading
import time
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# Live llama.cpp counters are sampled by the existing localhost dashboard.
# Slot IDs are NOT necessarily worker IDs; never attach a slot to an agent
# without explicit request attribution.
_LOCAL_INFERENCE_LOCK = threading.Lock()
_LOCAL_INFERENCE_PREVIOUS = {}
_LOCAL_INFERENCE_WINDOW = 3.0


def local_inference():
    """Read current model slot decode speeds, no inference and no billable calls.

    A separate poll endpoint keeps frequent llama.cpp checks out of the heavier
    SQLite /api/snapshot. Returned rates are generated tokens per wall second,
    sampled per actual llama.cpp slot. A missing decoded counter yields null
    rather than inventing a throughput number.
    """
    base = os.environ.get('SPORE_LOCAL_BASE_URL', 'http://127.0.0.1:8080/v1').rstrip('/')
    if base.endswith('/v1'):
        base = base[:-3]
    # Only probe the local inference service from this read-only dashboard.
    if base not in ('http://127.0.0.1:8080', 'http://localhost:8080'):
        return {'available':False, 'aggregate_tps':None, 'slots':[],
                'error':'Local inference endpoint not configured for loopback'}
    request = Request(base + '/slots', headers={'Accept':'application/json'})
    try:
        with urlopen(request, timeout=1.5) as reply:
            rows = json.load(reply)
    except (OSError, URLError, HTTPError, TimeoutError, ValueError):
        with _LOCAL_INFERENCE_LOCK:
            _LOCAL_INFERENCE_PREVIOUS.clear()
        return {'available':False,'aggregate_tps':None,'slots':[]}
    if not isinstance(rows, list):
        return {'available':False,'aggregate_tps':None,'slots':[]}
    now = time.monotonic()
    active_ids = set()
    output = []
    with _LOCAL_INFERENCE_LOCK:
        for fallback, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            sid = row.get('id',fallback)
            if type(sid) is not int:
                sid = fallback
            active_ids.add(sid)
            busy = bool(row.get('is_processing'))
            task = row.get('id_task')
            next_token = row.get('next_token')
            n = (next_token.get('n_decoded') if isinstance(next_token, dict)
                 else row.get('n_decoded'))
            if type(n) is not int or n < 0:
                n = None
            old = _LOCAL_INFERENCE_PREVIOUS.get(sid)
            # New task, prompt prefill and counter resets cannot count as
            # generated tokens from the preceding request.
            reset = (old is None or not busy or not old['busy'] or
                     task != old['task'] or n is None or old['n'] is None or
                     n < old['n'] or now < old['time'])
            if reset:
                start, samples = now, []
            else:
                start, samples = old['start'], old['samples']
                if n > old['n']:
                    samples.append((now, n - old['n']))
            samples = [(ts,delta) for ts,delta in samples
                       if now - ts <= _LOCAL_INFERENCE_WINDOW]
            elapsed = min(_LOCAL_INFERENCE_WINDOW,now-start)
            speed = (sum(delta for _,delta in samples)/elapsed
                     if busy and n is not None and elapsed > 0 else None)
            state = ('IDLE' if not busy else
                     'GENERATING' if speed and speed > 0 else
                     'PREFILL' if n == 0 else 'WAITING')
            _LOCAL_INFERENCE_PREVIOUS[sid] = {
                'busy':busy,'task':task,'n':n,'time':now,
                'start':start,'samples':samples}
            context = row.get('n_ctx')
            output.append({
                'id':sid,'state':state,
                'tps':round(speed,2) if speed is not None else None,
                'decoded_tokens':n,
                'context':context if type(context) is int else None})
        for sid in list(_LOCAL_INFERENCE_PREVIOUS):
            if sid not in active_ids:
                del _LOCAL_INFERENCE_PREVIOUS[sid]
    output.sort(key=lambda slot:slot['id'])
    rates = [x['tps'] for x in output if x['tps'] is not None]
    stage = None
    try:
        state = json.loads((Path.home()/'inkling-runtime/sad-parallel-live.json').read_text())
        if 0 <= time.time()-state.get('updated_epoch',0) < 30:
            stage = {
                'name':str(state.get('stage',''))[:40],
                'round':state.get('round'),
                'workers':state.get('workers'),
            }
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return {'available':True,'aggregate_tps':round(sum(rates),2) if rates else None,
            'slots':output,'sample_window_seconds':_LOCAL_INFERENCE_WINDOW,
            'benchmark':stage}


VA_RE = re.compile(r'0x[0-9a-fA-F]{8}\Z')
SECRET = re.compile(r'(?i)(sk-or-[A-Za-z0-9_\-]{6,}|(?:OPENROUTER_API_KEY|authorization|api[_-]?key)\s*[=:]\s*[^\s,;]+|Bearer\s+[^\s]+)')
DIFF_RE = re.compile(r'(?i)(?:\bdiffs?\s*[=:]\s*\d+|\boffsets?\s*[=:]\s*0x[\da-f,]+|\b(?:no match|mismatch|byte.exact)\b)')


def sanitize(value, max_chars=1200):
    return SECRET.sub('[REDACTED]', str(value or ''))[:max_chars]


@contextmanager
def connect_ro(repo):
    path = repo/'work/scratch/codex-pass1/spore.sqlite3'
    if not path.is_file():
        raise FileNotFoundError('SQLite database not initialized')
    db = sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=5)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA busy_timeout=5000')
    db.execute('PRAGMA query_only=ON')
    try:
        yield db
    finally:
        db.close()


def proc_running(pid):
    try:
        data = (Path('/proc')/str(int(pid))/'stat').read_text()
        return data.rsplit(')',1)[1].split()[0] not in ('Z','X')
    except (OSError, ValueError, IndexError, TypeError):
        return False


def _read_tail(path, max_bytes=120_000):
    try:
        with path.open('rb') as f:
            f.seek(0,2)
            size=f.tell()
            offset=max(0,size-max_bytes)
            f.seek(offset)
            data=f.read()
        if offset:
            data=data.split(b'\n',1)[-1]
        return data.decode('utf-8','replace').splitlines()
    except (OSError, ValueError):
        return []


def _inside_logs(repo, raw):
    if not raw:
        return None
    root = (repo/'work/scratch/codex-pass1/logs').resolve()
    file = Path(raw)
    if not file.is_absolute():
        file=repo/file
    try:
        file=file.resolve()
        if file.is_relative_to(root) and file.suffix=='.jsonl' and file.is_file():
            return file
    except (OSError, ValueError):
        pass
    return None


def _log_for_va(repo,va):
    if not isinstance(va,str) or not VA_RE.fullmatch(va):
        return None
    root=repo/'work/scratch/codex-pass1/logs'
    try:
        files=[p for p in root.glob(va[2:] + '.*.jsonl') if p.is_file()]
        return max(files,key=lambda x:x.stat().st_mtime_ns) if files else None
    except (OSError, ValueError):
        return None


def _event(event):
    if not isinstance(event,dict):
        return None
    kind=event.get('type')
    part=event.get('part') or {}
    if not isinstance(part,dict):
        part={}
    if kind=='text':
        text=part.get('text','')
        return {'kind':'text','message':sanitize(text,2000)} if text else None
    if kind=='reasoning':
        return {'kind':'reasoning','message':'Model reasoning step'}
    if kind=='tool_use':
        name=sanitize(part.get('tool') or 'tool',64)
        state=part.get('state') or {}
        status=state.get('status') if isinstance(state,dict) else None
        # Never return raw tool command inputs or shell output (may contain secrets).
        return {'kind':'tool','message':f'{name} · {sanitize(status or "activity",70)}'}
    if kind=='step_finish':
        tokens=part.get('tokens') or {}
        cost=part.get('cost')
        if isinstance(cost,(int,float)) and not isinstance(cost,bool) and 0<=cost<10000:
            return {'kind':'usage','message':f'Inference step · ${cost:.5f} · {tokens.get("output", 0)} output tokens'}
        return {'kind':'usage','message':'Inference step finished'}
    if kind=='retry':
        return {'kind':'provider','message':'Provider retry / backoff'}
    if kind=='error':
        error=event.get('error') or {}
        data=error.get('data') if isinstance(error,dict) else {}
        message=data.get('message') if isinstance(data,dict) else ''
        return {'kind':'error','message':sanitize(message or 'Agent error',240)}
    if kind=='step_start':
        return {'kind':'step','message':'Agent turn started'}
    return None


def stream(repo, path, limit=80):
    file=_inside_logs(repo,str(path)) if path else None
    if not file:
        return []
    events=[]
    for line in _read_tail(file,180_000 if limit>12 else 36_000):
        try:
            event=json.loads(line)
        except ValueError:
            continue
        item=_event(event)
        if item:
            events.append(item)
    return events[-limit:]


@lru_cache(maxsize=4096)
def _log_cost_cached(path, size, mtime_ns):
    """OpenCode observed step costs, a lower bound when final steps are missing."""
    total=0.0
    steps=0
    output=0
    try:
        with open(path,encoding='utf-8',errors='replace') as f:
            for line in f:
                if 'step_finish' not in line:
                    continue
                try:
                    event=json.loads(line)
                except ValueError:
                    continue
                if event.get('type')!='step_finish':
                    continue
                p=event.get('part') or {}
                cost=p.get('cost')
                if isinstance(cost,(int,float)) and not isinstance(cost,bool) and 0<=cost<10000:
                    total+=cost
                    steps+=1
                tok=p.get('tokens') or {}
                if isinstance(tok,dict):
                    output+=max(0,int(tok.get('output') or 0))
    except (OSError,ValueError,TypeError):
        return None
    return {'cost':round(total,9),'steps':steps,'output_tokens':output,'has_cost':steps>0}


def log_cost(repo, raw):
    file=_inside_logs(repo,raw)
    if not file:
        return {'cost':0.0,'steps':0,'output_tokens':0,'has_cost':False}
    try:
        st=file.stat()
        return _log_cost_cached(str(file),st.st_size,st.st_mtime_ns)
    except OSError:
        return {'cost':0.0,'steps':0,'output_tokens':0,'has_cost':False}


def latest_by_va(db):
    rows=db.execute('''SELECT a.va,a.id,a.status,a.effort,a.note,a.utc,a.log_path FROM attempts a
      LEFT JOIN attempt_reviews v ON v.attempt_id=a.id
      WHERE v.attempt_id IS NULL AND a.id=(
          SELECT MAX(b.id) FROM attempts b LEFT JOIN attempt_reviews x ON x.attempt_id=b.id
          WHERE b.va=a.va AND x.attempt_id IS NULL)''').fetchall()
    return {row['va']:dict(row) for row in rows}


def worker_info(repo,run_id,claimed,last_attempts=None,byte_sizes=None,active_slots=None):
    workers=[]
    if not run_id:return workers
    file=repo/'work/scratch/codex-pass1/parallel'/run_id/'run.json'
    try:
        processes=json.loads(file.read_text()).get('processes',[])
    except (OSError,ValueError):
        processes=[]
    for item in processes:
        i=item.get('worker')
        if not isinstance(i,int):continue
        pid=int(item.get('pid') or 0)
        va=claimed.get(i)
        last_va=va or (last_attempts or {}).get(i)
        slots=active_slots or {}
        # Active SQLite slot state, not stale lines in the last agent log.
        phase=('VERIFIED' if slots.get(va) else 'PROBING') if va in slots else ('NO SLOT' if va else 'IDLE')
        logfile=_log_for_va(repo,last_va)
        view=stream(repo,logfile,limit=4) if logfile and phase in ('VERIFIED','PROBING') else []
        workers.append({'worker':i+1,'pid':pid,'alive':proc_running(pid),
                        'current':va,'last_function':last_va,'phase':phase,
                        'bytes':(byte_sizes or {}).get(last_va), 'stream':view})
    return workers


def cost_analysis(repo,db,latest):
    """Cumulative per-function attempted spend split by FINAL valid status.

    A function that failed twice then matched counts all three passes against
    MATCH. This is the useful cost-to-achieve-outcome, not only the final pass.
    These totals exclude provider-only attempts missing from attempts.
    """
    per_va=defaultdict(lambda:{'cost':0.0,'attempts':0,'covered':0,'steps':0})
    for row in db.execute('''SELECT a.va,a.log_path FROM attempts a
                        LEFT JOIN attempt_reviews r ON r.attempt_id=a.id
                        WHERE r.attempt_id IS NULL'''):
        z=per_va[row['va']]
        z['attempts']+=1
        c=log_cost(repo,row['log_path'])
        z['cost']+=c['cost']
        z['covered']+=int(c['has_cost'])
        z['steps']+=c['steps']
    groups={key:{'functions':0,'cost':0.0,'attempts':0,'covered':0,'steps':0} for key in ('MATCH','FAIL','DEFER','SKIP')}
    for va, row in latest.items():
        status=row['status']
        if status not in groups:continue
        g=groups[status]
        value=per_va[va]
        g['functions']+=1
        g['cost']+=value['cost']
        g['attempts']+=value['attempts']
        g['covered']+=value['covered']
        g['steps']+=value['steps']
    for g in groups.values():
        g['cost']=round(g['cost'],7)
        g['per_function']=round(g['cost']/g['functions'],7) if g['functions'] else None
    return groups


def snapshot(repo):
    with connect_ro(repo) as db:
        latest=latest_by_va(db)
        by_status=Counter(row['status'] for row in latest.values())
        effort=Counter(row['effort'] for row in latest.values() if row['status']=='FAIL')
        byte_sizes={r['va']:int(r['size']) for r in db.execute('SELECT va,size FROM queue')}
        byte_totals=Counter()
        effort_bytes=Counter()
        for va,size in byte_sizes.items():
            kind=latest[va]['status'] if va in latest else 'PENDING'
            byte_totals[kind]+=size
            if kind=='FAIL':
                effort_bytes[latest[va]['effort']]+=size
        byte_totals['queue']=sum(byte_sizes.values())
        scalar=lambda sql,*a:db.execute(sql,a).fetchone()[0]
        last=db.execute('SELECT * FROM runs ORDER BY started_utc DESC LIMIT 1').fetchone()
        rid=last['run_id'] if last else None
        claims={r['worker_idx']:r['va'] for r in db.execute('SELECT worker_idx,va FROM claims WHERE run_id=?',(rid,))} if rid else {}
        # Telemetry must remain readable for databases created before the
        # verified-token schema migration. Setup upgrades the schema, but a
        # read-only dashboard must not require it just to show old results.
        slot_columns={row['name'] for row in db.execute('PRAGMA table_info(inference_slots)')}
        if rid and 'verified' in slot_columns:
            active_slots={r['va']:bool(r['verified']) for r in db.execute(
                'SELECT va,verified FROM inference_slots WHERE run_id=?',(rid,))}
        elif rid:
            active_slots={r['va']:False for r in db.execute(
                'SELECT va FROM inference_slots WHERE run_id=?',(rid,))}
        else:
            active_slots={}
        last_by_worker={}
        if rid:
            for x in db.execute('SELECT worker_idx,va FROM attempts WHERE run_id=? AND worker_idx IS NOT NULL ORDER BY id',(rid,)):
                last_by_worker[x['worker_idx']]=x['va']
        counts={'queue':scalar('SELECT COUNT(*) FROM queue'), 'matches':by_status.get('MATCH',0),
                'fails':by_status.get('FAIL',0),'deferred':by_status.get('DEFER',0),
                'skips':by_status.get('SKIP',0),'pending':scalar('SELECT COUNT(*) FROM queue')-len(latest),
                'void_provider':scalar('SELECT COUNT(*) FROM attempt_reviews'),
                'incidents':scalar('SELECT COUNT(*) FROM provider_incidents'),
                'claims':scalar('SELECT COUNT(*) FROM claims'),
                'inference':scalar('SELECT COUNT(*) FROM inference_slots')}
        run={}
        if last:
            # Outcome and progress data MUST refer to this run's assignments
            # and valid attempts. Previously failed attempts belong to the
            # historical totals, not to the new passes granted for this run.
            targets=db.execute('''SELECT x.va,q.size FROM assignments x
                JOIN queue q ON q.va=x.va WHERE x.run_id=?''',(rid,)).fetchall()
            run_latest={}
            valid_attempts=0
            for attempt in db.execute('''SELECT t.va,t.status FROM attempts t
                    LEFT JOIN attempt_reviews v ON v.attempt_id=t.id
                    WHERE t.run_id=? AND v.attempt_id IS NULL ORDER BY t.id''',(rid,)):
                run_latest[attempt['va']]=attempt['status']
                valid_attempts+=1

            # An older database may not have this table. New runs snapshot a
            # per-function lifetime ceiling at launch, so last['passes']
            # must NOT be used alone for their completion calculation.
            has_caps=db.execute("""SELECT 1 FROM sqlite_master
                                   WHERE type='table' AND name='run_attempt_limits'""").fetchone()
            caps={row['va']:int(row['max_attempts']) for row in db.execute(
                'SELECT va,max_attempts FROM run_attempt_limits WHERE run_id=?',(rid,)
            )} if has_caps else {}
            attempts_used={row['va']:int(row['n']) for row in db.execute('''
                SELECT t.va,COUNT(*) AS n FROM attempts t
                LEFT JOIN attempt_reviews v ON v.attempt_id=t.id
                JOIN assignments x ON x.va=t.va AND x.run_id=?
                WHERE t.status IN ('FAIL','DEFER') AND v.attempt_id IS NULL
                GROUP BY t.va''',(rid,))}
            excluded={row['va'] for row in db.execute(
                'SELECT va FROM run_exclusions WHERE run_id=?',(rid,))}
            statuses=('MATCH','FAIL','DEFER','SKIP','PENDING')
            outcomes={key:0 for key in statuses}
            outcome_bytes={key:0 for key in statuses}
            completed=completed_bytes=target_bytes=0
            for target_row in targets:
                va=target_row['va']
                size=int(target_row['size'])
                target_bytes+=size
                status=run_latest.get(va,'PENDING')
                if status not in outcomes:
                    status='PENDING'
                outcomes[status]+=1
                outcome_bytes[status]+=size
                ceiling=caps.get(va,int(last['passes']))
                terminal=(status in ('MATCH','SKIP')
                    or va in excluded
                    or (status in ('FAIL','DEFER')
                        and attempts_used.get(va,0)>=ceiling))
                if terminal:
                    completed+=1
                    completed_bytes+=size
            archived_provider=db.execute('''SELECT COUNT(*) FROM attempts t
                JOIN attempt_reviews v ON v.attempt_id=t.id
                WHERE t.run_id=?''',(rid,)).fetchone()[0]
            run_incidents=db.execute(
                'SELECT COUNT(*) FROM provider_incidents WHERE run_id=?',(rid,)
            ).fetchone()[0]
            # Distinguish genuine new work from retries already attempted before
            # this expedition. Previous runs are not charged to the current one.
            target_addresses={row['va'] for row in targets}
            before={}
            for row in db.execute('''SELECT t.va,t.status FROM attempts t
                JOIN assignments x ON x.va=t.va AND x.run_id=?
                LEFT JOIN attempt_reviews v ON v.attempt_id=t.id
                WHERE (t.run_id IS NULL OR t.run_id<>?)
                  AND v.attempt_id IS NULL ORDER BY t.id''',(rid,rid)):
                if row['va'] in target_addresses:
                    before[row['va']]=row['status']
            initial={key:0 for key in ('MATCH','FAIL','DEFER','SKIP','PENDING')}
            initial_bytes={key:0 for key in initial}
            for row in targets:
                key=before.get(row['va'],'PENDING')
                if key not in initial:
                    key='PENDING'
                initial[key]+=1
                initial_bytes[key]+=int(row['size'])
            # Scope selection is saved at dashboard launch, but a standalone
            # run may have different settings. Do not infer a module from
            # the current dropdown or mislabel an older expedition.
            runtime={}
            try:
                record=repo/'work/scratch/codex-pass1/parallel/latest.json'
                runtime=json.loads(record.read_text(encoding='utf-8'))
                if runtime.get('run_id')!=rid:
                    runtime={}
            except (OSError,ValueError,TypeError):
                runtime={}
            run={'id':rid,'targets':len(targets),'completed':completed,
                 'bytes_target':target_bytes,'bytes_completed':completed_bytes,
                 'workers':last['workers'],'passes':last['passes'],
                 'max_inflight':runtime.get('max_inflight'),
                 'idle_minutes':runtime.get('minutes'),
                 'initial_outcomes':initial,'initial_bytes':initial_bytes,
                 'finished':bool(last['finished_utc']),
                 'started':last['started_utc'],'finished_at':last['finished_utc'],
                 'outcomes':outcomes,'outcome_bytes':outcome_bytes,
                 'valid_attempts':valid_attempts,'archived_provider':archived_provider,
                 'provider_incidents':run_incidents,'excluded':len(excluded)}
        a=db.execute('SELECT * FROM adaptive_control WHERE singleton=1').fetchone()
        adaptive={}
        if a and a['run_id']==rid:
            adaptive={'concurrency':a['concurrency'],'ceiling':a['ceiling'],
                      'cooldown_seconds':max(0,int(a['cooldown_until']-datetime.now(timezone.utc).timestamp())),
                      'provider_failures':a['provider_failures'],'productive_passes':a['productive_samples']}
        b=db.execute('SELECT * FROM budget WHERE singleton=1').fetchone()
        # Preserve lifetime accounting, but display this RUN's separate
        # spending limit. Historical runs predate the additive run_budgets
        # migration; they have no recorded run baseline.
        has_run_budget=db.execute("""SELECT 1 FROM sqlite_master
                                    WHERE type='table' AND name='run_budgets'""").fetchone()
        rb=(db.execute('SELECT * FROM run_budgets WHERE run_id=?',(rid,)).fetchone()
            if rid and has_run_budget else None)
        budget={'spend':max(0,b['last_usage']-b['baseline_usage']) if b else None,
                'run_spend':max(0,rb['last_usage']-rb['baseline_usage']) if rb else None,
                'run_limit':rb['budget_limit'] if rb else None,
                'inflight':scalar('SELECT COALESCE(SUM(amount),0) FROM reservations WHERE run_id=?',rid)
                            if rid else 0.0}
        events=[]
        for r in db.execute('''SELECT va,status,utc,worker_idx,id FROM attempts
              WHERE id NOT IN (SELECT attempt_id FROM attempt_reviews) ORDER BY id DESC LIMIT 32'''):
            events.append({'va':r['va'],'kind':r['status'],'time':r['utc'],
                           'worker':None if r['worker_idx'] is None else r['worker_idx']+1})
        for r in db.execute('SELECT va,utc,worker_idx,id FROM provider_incidents ORDER BY id DESC LIMIT 32'):
            events.append({'va':r['va'],'kind':'PROVIDER','time':r['utc'],
                           'worker':None if r['worker_idx'] is None else r['worker_idx']+1})
        events=sorted(events,key=lambda r:r['time'],reverse=True)[:50]
        costs=cost_analysis(repo,db,latest)
        # Costs are covered only to the extent the CLI emitted step_finish records.
        from spore_dashboard_control import modules as known_modules
        module_names=known_modules(repo)
    workers=worker_info(repo,rid,claims,last_by_worker,byte_sizes,active_slots=active_slots)
    run['live_workers']=sum(w['alive'] for w in workers)
    if not rid:
        run['state']='none'
    elif run['finished']:
        # Finished may also mean budget, watchdog or manual shutdown.
        # Do not claim success for a run that ended with unfinished targets.
        run['state']='completed' if run['completed']==run['targets'] else 'ended'
    elif run['live_workers']:
        run['state']='running'
    else:
        run['state']='inactive'
    return {'generated_utc':datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'run':run,'workers':workers,'events':events,'counters':counts,'budget':budget,
            'adaptive':adaptive,'effort':{f'+{i}':effort.get(f'+{i}',0) for i in range(1,6)},
            'costs':costs,'modules':module_names,
            'bytes':dict(byte_totals),
            'effort_bytes':{f'+{i}':effort_bytes.get(f'+{i}',0) for i in range(1,6)}}



# The attempt note's checker suffix is generated by the runner's independent
# verification. Identical compiled/retail lengths are required to calculate
# byte agreement from a reported differing-byte count.
CHECKER_SIZE = re.compile(r'\bsize\s*[=:]\s*(\d+)\s*/\s*(\d+)\b', re.I)
CHECKER_DIFFS = re.compile(r'\b(?:diffs?|differences)\s*[=:]\s*(\d+)\b', re.I)


def failure_reason(note,max_chars=280):
    """Expose the actual recorded technical blocker, separate from checker output."""
    note = str(note or '').strip()
    reason = note.split(' | checker:', 1)[0]
    reason = re.sub(r'^Pass\s+\d+\s*:\s*', '', reason, flags=re.I)
    return sanitize(reason, max_chars)


def byte_agreement(status, expected_size, note):
    """Report equal-size byte agreement; never confuse size with matching bytes."""
    total = int(expected_size)
    if status == 'MATCH':
        return {'matched': total, 'total': total, 'diffs': 0, 'kind': 'verified'}
    if status not in ('FAIL', 'DEFER'):
        return None
    text = str(note or '')
    if ' | checker:' in text:
        suffix = text.rsplit(' | checker:', 1)[-1]
    elif status == 'DEFER':
        # Timeout notes historically used "Pass N: time limit (5 min); <checker>"
        # rather than the FAIL/regular " | checker:" separator.
        # Only parse the independent-check section, never arbitrary model text.
        timeout_note = re.search(r'\btime limit\s*\([^)]*\)\s*;\s*(.*)', text, re.I | re.S)
        suffix = timeout_note.group(1) if timeout_note else ''
    else:
        suffix = ''
    sizes = list(CHECKER_SIZE.finditer(suffix))
    diffs = list(CHECKER_DIFFS.finditer(suffix))
    if not sizes:
        return None
    compiled, retail = map(int, sizes[-1].groups())
    if compiled != retail or retail != total:
        return {'matched': None, 'total': total, 'kind': 'size_mismatch',
                'compiled_size': compiled, 'retail_size': retail}
    if not diffs:
        return None
    n = int(diffs[-1].group(1))
    if n > total:
        return None
    return {'matched': total - n, 'total': total, 'diffs': n, 'kind': 'independent_checker'}


@lru_cache(maxsize=4096)
def _logged_agreement_cached(filename, file_size, mtime_ns, status, expected_size):
    """Last genuine check.py tool output in an OpenCode log.

    Older independent verifier results retained only a headline; this may
    reflect an intermediate agent check, not necessarily the final edit.
    """
    last = None
    for line in _read_tail(Path(filename), 250_000):
        if 'tool_use' not in line or 'check.py' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get('type') != 'tool_use':
            continue
        part = event.get('part') or {}
        if not isinstance(part, dict):
            continue
        state = part.get('state') or {}
        if not isinstance(state, dict):
            continue
        inputs = state.get('input') or {}
        cmd = str(inputs.get('command') or inputs.get('cmd') or '') if isinstance(inputs, dict) else ''
        if 'check.py' not in cmd:
            continue
        output = state.get('output') or ''
        if not isinstance(output, str):
            continue
        guess = byte_agreement(status, expected_size, ' | checker:' + output)
        if guess is not None:
            guess['kind'] = 'logged_checker' if guess['matched'] is not None else 'size_mismatch'
            guess['origin'] = 'agent_tool_log'
            last = guess
    return last


def logged_byte_agreement(repo, raw, status, expected_size):
    path = _inside_logs(repo, raw)
    if not path or status not in ('FAIL', 'DEFER'):
        return None
    try:
        st = path.stat()
        return _logged_agreement_cached(str(path), st.st_size, st.st_mtime_ns,
                                         status, int(expected_size))
    except OSError:
        return None

def functions(repo,status='ALL',search='',offset=0,limit=75):
    status=status.upper()
    if status not in ('ALL','MATCH','FAIL','DEFER','SKIP','PENDING'):raise ValueError('Invalid status')
    if not isinstance(search,str) or len(search)>96:raise ValueError('Invalid search')
    if not isinstance(offset,int) or not 0<=offset<=100000:raise ValueError('Invalid offset')
    if not isinstance(limit,int) or not 1<=limit<=150:raise ValueError('Invalid limit')
    with connect_ro(repo) as db:
        latest=latest_by_va(db)
        attempts=Counter(r['va'] for r in db.execute('''SELECT a.va FROM attempts a LEFT JOIN attempt_reviews v ON v.attempt_id=a.id WHERE v.attempt_id IS NULL'''))
        rows=[]
        for r in db.execute('SELECT va,position,size,suggested_file FROM queue ORDER BY position'):
            x=latest.get(r['va'])
            kind=x['status'] if x else 'PENDING'
            if status!='ALL' and status!=kind:continue
            if search and search.lower() not in (r['va']+' '+(r['suggested_file'] or '')).lower():continue
            rows.append({'va':r['va'],'size':r['size'],'file':sanitize(r['suggested_file'],180),
                         '_last_log':x['log_path'] if x else None,
                         '_last_id':x['id'] if x else -1,
                         'last_activity':x['utc'] if x else None,
                         'status':kind,'effort':x['effort'] if x else '',
                         'reason':failure_reason(x['note']) if x and kind in ('FAIL','DEFER','SKIP') else '',
                         'agreement':byte_agreement(kind,r['size'],x['note'] if x else ''),
                         'attempts':attempts[r['va']]})
        # Sort across the full filtered result set before pagination. Pending
        # functions have no activity and remain at the bottom in queue order.
        rows.sort(key=lambda item: (-item['_last_id'], item['va']))
        items=rows[offset:offset+limit]
        for item in items:
            if item['agreement'] is None:
                item['agreement']=logged_byte_agreement(repo,item['_last_log'],item['status'],item['size'])
            item.pop('_last_log',None)
            item.pop('_last_id',None)
        return {'total':len(rows),'bytes':sum(r['size'] for r in rows),
                'offset':offset,'items':items}


def _checker_output(repo,raw):
    path=_inside_logs(repo,raw)
    if not path:return {'summary':[], 'evidence':False}
    findings=[]
    for line in _read_tail(path,150_000):
        if 'tool_use' not in line:continue
        try:
            event=json.loads(line)
        except ValueError:continue
        if event.get('type')!='tool_use':continue
        part=event.get('part') or {}
        state=part.get('state') or {}
        inp=state.get('input') or {}
        cmd=str(inp.get('command') or inp.get('cmd') or '')
        if 'check.py' not in cmd:continue
        result=state.get('output') or ''
        if not isinstance(result,str):continue
        for output in result.splitlines():
            if DIFF_RE.search(output):findings.append(sanitize(output,240))
    return {'summary':findings[-24:],'evidence':bool(findings)}


def function_detail(repo,va):
    if not VA_RE.fullmatch(va):raise ValueError('Invalid function address')
    with connect_ro(repo) as db:
        row=db.execute('SELECT va,size,suggested_file FROM queue WHERE va=?',(va,)).fetchone()
        if row is None:raise ValueError('Function not in queue')
        history=[]
        for a in db.execute('''SELECT a.id,a.va,a.pass_number,a.status,a.effort,a.note,a.utc,a.log_path,
             r.classification FROM attempts a LEFT JOIN attempt_reviews r ON r.attempt_id=a.id
             WHERE a.va=? ORDER BY a.id''',(va,)):
            history.append({'id':a['id'],'pass':a['pass_number'],'status':a['classification'] or a['status'],
                            'effort':a['effort'],'note':sanitize(a['note'],1400),'time':a['utc'],
                            'cost':log_cost(repo,a['log_path'])})
        last=db.execute('''SELECT a.status,a.note,a.log_path FROM attempts a LEFT JOIN attempt_reviews r ON r.attempt_id=a.id
            WHERE a.va=? AND r.attempt_id IS NULL ORDER BY a.id DESC LIMIT 1''',(va,)).fetchone()
        live=db.execute('SELECT 1 FROM claims WHERE va=?',(va,)).fetchone() is not None
    log=_log_for_va(repo,va) if live else (_inside_logs(repo,last['log_path']) if last else None)
    checker=_checker_output(repo,str(log) if log else None)
    verified=any(h['status']=='MATCH' for h in history if h['status']!='VOID_PROVIDER')
    return {'va':va,'size':row['size'],'file':sanitize(row['suggested_file'],220),
            'history':history,'current':live,'checker':checker,
            'reason':failure_reason(last['note'],1200) if last and last['status'] in ('FAIL','DEFER','SKIP') else '',
            'agreement':(byte_agreement(last['status'] if last else 'PENDING',row['size'],last['note'] if last else '') or
                         logged_byte_agreement(repo,last['log_path'] if last else None,
                                               last['status'] if last else 'PENDING',row['size'])),
            'diff_status':'MATCH: zero remaining byte differences' if verified else (
              'Saved checker mismatch evidence' if checker['evidence'] else 'No verified byte-level diff available')}


def agent_detail(repo,worker):
    if not isinstance(worker,int) or not 1<=worker<=32:raise ValueError('Invalid worker index')
    with connect_ro(repo) as db:
        last=db.execute('SELECT run_id FROM runs ORDER BY started_utc DESC LIMIT 1').fetchone()
        rid=last['run_id'] if last else None
        claim=db.execute('SELECT va FROM claims WHERE run_id=? AND worker_idx=?',(rid,worker-1)).fetchone() if rid else None
        va=claim['va'] if claim else None
        if not va and rid:
            r=db.execute('''SELECT va FROM attempts WHERE run_id=? AND worker_idx=? ORDER BY id DESC LIMIT 1''',(rid,worker-1)).fetchone()
            if r:va=r['va']
        size=db.execute('SELECT size FROM queue WHERE va=?',(va,)).fetchone() if va else None
        byte_count=size['size'] if size else None
    logfile=_log_for_va(repo,va)
    workerlog=repo/'work/scratch/codex-pass1/parallel'/rid/f'worker-{worker:02d}.log' if rid else None
    status=[]
    if workerlog and workerlog.is_file():
        status=[sanitize(x,220) for x in _read_tail(workerlog,6000)[-9:] if x.strip()]
    return {'worker':worker,'va':va,'bytes':byte_count,
            'events':stream(repo,logfile,limit=100) if logfile else [],
            'log_status':status,'has_stream':bool(logfile)}
