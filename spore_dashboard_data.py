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
    rows=db.execute('''SELECT a.va,a.id,a.status,a.effort,a.note,a.utc FROM attempts a
      LEFT JOIN attempt_reviews v ON v.attempt_id=a.id
      WHERE v.attempt_id IS NULL AND a.id=(
          SELECT MAX(b.id) FROM attempts b LEFT JOIN attempt_reviews x ON x.attempt_id=b.id
          WHERE b.va=a.va AND x.attempt_id IS NULL)''').fetchall()
    return {row['va']:dict(row) for row in rows}


def worker_info(repo,run_id,claimed,last_attempts=None):
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
        logfile=_log_for_va(repo,va or (last_attempts or {}).get(i))
        if logfile:
            view=stream(repo,logfile,limit=4)
        else:
            view=[]
        workers.append({'worker':i+1,'pid':pid,'alive':proc_running(pid),
                        'current':va,'stream':view})
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
        scalar=lambda sql,*a:db.execute(sql,a).fetchone()[0]
        last=db.execute('SELECT * FROM runs ORDER BY started_utc DESC LIMIT 1').fetchone()
        rid=last['run_id'] if last else None
        claims={r['worker_idx']:r['va'] for r in db.execute('SELECT worker_idx,va FROM claims WHERE run_id=?',(rid,))} if rid else {}
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
            target=scalar('SELECT COUNT(*) FROM assignments WHERE run_id=?',rid)
            completed=scalar('''SELECT COUNT(*) FROM assignments x WHERE x.run_id=? AND (
              (SELECT t.status FROM attempts t LEFT JOIN attempt_reviews v ON v.attempt_id=t.id
                WHERE t.va=x.va AND v.attempt_id IS NULL ORDER BY t.id DESC LIMIT 1) IN ('MATCH','SKIP')
              OR (SELECT COUNT(*) FROM attempts t LEFT JOIN attempt_reviews v ON v.attempt_id=t.id
                WHERE t.va=x.va AND t.status IN ('FAIL','DEFER') AND v.attempt_id IS NULL)>=?
              OR EXISTS(SELECT 1 FROM run_exclusions e WHERE e.run_id=x.run_id AND e.va=x.va))''',rid,last['passes'])
            run={'id':rid,'targets':target,'completed':completed,'workers':last['workers'],
                 'finished':bool(last['finished_utc']),'started':last['started_utc']}
        a=db.execute('SELECT * FROM adaptive_control WHERE singleton=1').fetchone()
        adaptive={}
        if a and a['run_id']==rid:
            adaptive={'concurrency':a['concurrency'],'ceiling':a['ceiling'],
                      'cooldown_seconds':max(0,int(a['cooldown_until']-datetime.now(timezone.utc).timestamp())),
                      'provider_failures':a['provider_failures'],'productive_passes':a['productive_samples']}
        b=db.execute('SELECT * FROM budget WHERE singleton=1').fetchone()
        budget={'spend':max(0,b['last_usage']-b['baseline_usage']) if b else None,
                'inflight':scalar('SELECT COALESCE(SUM(amount),0) FROM reservations')}
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
    workers=worker_info(repo,rid,claims,last_by_worker)
    run['live_workers']=sum(w['alive'] for w in workers)
    return {'generated_utc':datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'run':run,'workers':workers,'events':events,'counters':counts,'budget':budget,
            'adaptive':adaptive,'effort':{f'+{i}':effort.get(f'+{i}',0) for i in range(1,6)},
            'costs':costs,'modules':module_names}


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
                         'status':kind,'effort':x['effort'] if x else '',
                         'attempts':attempts[r['va']]})
        return {'total':len(rows),'offset':offset,'items':rows[offset:offset+limit]}


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
                            'effort':a['effort'],'note':sanitize(a['note'],320),'time':a['utc'],
                            'cost':log_cost(repo,a['log_path'])})
        last=db.execute('''SELECT a.log_path FROM attempts a LEFT JOIN attempt_reviews r ON r.attempt_id=a.id
            WHERE a.va=? AND r.attempt_id IS NULL ORDER BY a.id DESC LIMIT 1''',(va,)).fetchone()
        live=db.execute('SELECT 1 FROM claims WHERE va=?',(va,)).fetchone() is not None
    log=_log_for_va(repo,va) if live else (_inside_logs(repo,last['log_path']) if last else None)
    checker=_checker_output(repo,str(log) if log else None)
    verified=any(h['status']=='MATCH' for h in history if h['status']!='VOID_PROVIDER')
    return {'va':va,'size':row['size'],'file':sanitize(row['suggested_file'],220),
            'history':history,'current':live,'checker':checker,
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
    logfile=_log_for_va(repo,va or (last_attempts or {}).get(i))
    workerlog=repo/'work/scratch/codex-pass1/parallel'/rid/f'worker-{worker:02d}.log' if rid else None
    status=[]
    if workerlog and workerlog.is_file():
        status=[sanitize(x,220) for x in _read_tail(workerlog,6000)[-9:] if x.strip()]
    return {'worker':worker,'va':va,'events':stream(repo,logfile,limit=100) if logfile else [],
            'log_status':status,'has_stream':bool(logfile)}
