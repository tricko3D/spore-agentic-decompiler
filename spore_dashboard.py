"""Read-only localhost status board for Spore Agentic Decompiler.

No web dependencies, no write API, no raw prompts, keys, or unfiltered log text.
"""
from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import urlparse

PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Spore Agentic • Live</title>
<style>
:root{color-scheme:dark;font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;background:#101216;color:#e7e9ee}
*{box-sizing:border-box}body{max-width:1500px;margin:0 auto;padding:28px 22px 60px}h1{font-size:25px;letter-spacing:-.7px;margin:0}h2{font-size:15px;font-weight:600;margin:0 0 12px}p{margin:0;color:#9ca4b3}
.top{display:flex;gap:20px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin-bottom:23px}
.live{display:inline-flex;align-items:center;gap:8px;color:#b6bdc9;font-size:12px}.dot{height:8px;width:8px;border-radius:50%;background:#72daa7;box-shadow:0 0 0 4px #72daa71c}.dot.off{background:#ddaa63;box-shadow:0 0 0 4px #ddaa631c}
.metrics{display:grid;grid-template-columns:repeat(5,minmax(130px,1fr));gap:12px;margin-bottom:16px}
.card,.panel{border:1px solid #292e37;background:#191c22;border-radius:12px;padding:17px}
.metric{font-size:27px;font-weight:680;letter-spacing:-1px;margin:5px 0 2px;white-space:nowrap}.muted{font-size:12px;color:#99a2b2}.accent{color:#72daa7}.warning{color:#e9be78}.panel-grid{display:grid;grid-template-columns:1.1fr 1fr;gap:16px}.panel{min-width:0}
.usage{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px;flex-wrap:wrap}.bar{height:9px;border-radius:10px;background:#353a45;overflow:hidden}.bar>span{display:block;height:100%;background:#72daa7;transition:width .3s}
table{width:100%;border-collapse:collapse;text-align:left}th{color:#909aaa;font-size:11px;font-weight:500;white-space:nowrap}th,td{padding:10px 9px;border-bottom:1px solid #2a3039}td{font-size:12px}td.mono,.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}td:last-child{text-align:right}tbody tr:last-child td{border-bottom:0}
.pill{font-size:11px;padding:3px 7px;border-radius:6px;background:#323947;color:#d4dbe7;white-space:nowrap}.pill.match{background:#193d2c;color:#8de6bb}.pill.provider{background:#493725;color:#f1c38d}.pill.fail{background:#3c2b33;color:#e7a0a8}.pill.active{background:#243549;color:#a2d4ff}
.overflow{max-height:440px;overflow-y:auto}.foot{margin-top:17px;color:#8b94a2;font-size:12px}a{color:#9ac5fc}.small{font-size:12px}#error{color:#e7a0a8;margin-top:8px}
@media(max-width:1050px){.metrics{grid-template-columns:repeat(3,minmax(100px,1fr))}.panel-grid{grid-template-columns:1fr}}@media(max-width:600px){body{padding:18px 10px}.metrics{grid-template-columns:repeat(2,minmax(130px,1fr));gap:8px}.card,.panel{padding:12px}.metric{font-size:22px}h1{font-size:21px}}
</style></head><body>
<div class="top"><div><h1>Spore Agentic <span style="color:#8a92a0;font-weight:400">/ Live</span></h1><p>Local decompilation activity, read-only</p></div><div><span class="live"><span id="dot" class="dot"></span><span id="connection">Connecting…</span></span><p class="small" id="refresh">Updates every 3 seconds</p></div></div>
<div class="metrics"><div class="card"><div class="muted">Verified matches</div><div class="metric accent" id="matches">–</div><div class="muted">Recorded MATCH</div></div><div class="card"><div class="muted">Selected targets</div><div class="metric" id="targets">–</div><div class="muted" id="run">Latest run</div></div><div class="card"><div class="muted">Live workers</div><div class="metric" id="workers">–</div><div class="muted" id="activeClaims">– claimed functions</div></div><div class="card"><div class="muted">Sail concurrency</div><div class="metric" id="cap">–</div><div class="muted" id="cooldown">Adaptive cap</div></div><div class="card"><div class="muted">Budget used</div><div class="metric" id="spend">–</div><div class="muted" id="budget">OpenRouter estimate</div></div></div>
<div class="panel" style="margin-bottom:16px"><div class="usage"><h2 style="margin:0">Current run progress</h2><span class="muted" id="runStatus">–</span></div><div class="bar"><span id="progress" style="width:0%"></span></div><p class="foot" id="progressDetails">Waiting for data…</p></div>
<div class="panel-grid"><div class="panel"><h2>Workers</h2><div class="overflow"><table><thead><tr><th>Worker</th><th>PID</th><th>Current function</th><th>State</th></tr></thead><tbody id="workerRows"></tbody></table></div></div><div class="panel"><h2>Recent events</h2><div class="overflow"><table><thead><tr><th>Time (UTC)</th><th>Function</th><th>Event</th><th>Worker</th></tr></thead><tbody id="eventRows"></tbody></table></div></div></div>
<div class="foot">Only aggregate status and result metadata are served. API keys, raw OpenCode logs, prompts and model responses stay on disk. <span id="error"></span></div>
<script>
const $=id=>document.getElementById(id);
const esc=x=>String(x??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const str=(id,x)=>$(id).textContent=x;
const usd=x=>typeof x==='number'?'$'+x.toFixed(3):'–';
function render(d){
 const c=d.counters||{},r=d.run||{},a=d.adaptive||{},b=d.budget||{};
 str('matches',c.matches??0);str('targets',r.targets??0);str('run',r.id||'No run recorded');
 str('workers',(r.live_workers??0)+' / '+(r.workers??0));str('activeClaims',(c.claims??0)+' claimed functions');
 str('cap',a.concurrency!=null?(a.concurrency+' / '+a.ceiling):'–');
 str('cooldown',a.cooldown_seconds>0?('Cooldown '+a.cooldown_seconds+'s'):'No provider cooldown');
 str('spend',usd(b.spend));str('budget',b.inflight!=null?usd(b.inflight)+' reserved':'OpenRouter estimate');
 const done=r.completed??0, total=r.targets??0;
 $('progress').style.width=(total?100*done/total:0)+'%';
 str('runStatus',r.finished?'Finished':r.live_workers?'Running':'Idle / stopped');
 str('progressDetails',`${done} / ${total} targets completed or exhausted · ${c.void_provider??0} historical provider-only passes · ${c.incidents??0} provider incidents`);
 const workers=d.workers||[];
 $('workerRows').innerHTML=workers.length?workers.map(w=>`<tr><td class="mono">${esc(w.worker)}</td><td class="mono">${esc(w.pid)}</td><td class="mono">${esc(w.current||'–')}</td><td><span class="pill ${w.alive?'active':''}">${w.alive?'Active':'Stopped'}</span></td></tr>`).join(''):'<tr><td colspan="4">No workers recorded</td></tr>';
 const ev=d.events||[];
 $('eventRows').innerHTML=ev.length?ev.map(e=>`<tr><td class="mono">${esc((e.time||'').replace('T',' ').slice(0,19))}</td><td class="mono">${esc(e.va)}</td><td><span class="pill ${esc(e.kind.toLowerCase())}">${esc(e.kind)}</span></td><td>${esc(e.worker??'–')}</td></tr>`).join(''):'<tr><td colspan="4">No recorded events</td></tr>';
 str('connection','Connected · '+new Date().toLocaleTimeString());$('dot').classList.remove('off');str('error','');
}
async function poll(){try{const res=await fetch('/api/snapshot',{cache:'no-store'});if(!res.ok)throw Error('HTTP '+res.status);render(await res.json());}catch(e){str('connection','Disconnected');$('dot').classList.add('off');str('error',e.message)}}
poll();setInterval(poll,3000);
</script></body></html>'''


def _process_running(pid: int) -> bool:
    try:
        # A zombie must not count as an active worker.
        stat = (Path('/proc')/str(pid)/'stat').read_text()
        return stat.rsplit(')', 1)[1].split()[0] not in ('Z', 'X')
    except (OSError, ValueError, IndexError):
        return False


def snapshot(repo: Path) -> dict:
    work = repo / 'work/scratch/codex-pass1'
    dbfile = work / 'spore.sqlite3'
    if not dbfile.is_file():
        return {'error': 'SQLite database not initialized', 'run': {}, 'workers': [], 'events': []}
    # Read-only SQLite connection, compatible with live WAL writers.
    db = sqlite3.connect(dbfile.as_uri() + '?mode=ro', uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        with db:
            scalar = lambda sql, *params: db.execute(sql, params).fetchone()[0]
            last = db.execute('SELECT * FROM runs ORDER BY started_utc DESC LIMIT 1').fetchone()
            run_id = last['run_id'] if last else None
            statuses = db.execute('''SELECT a.status, COUNT(*) AS total FROM attempts a
                LEFT JOIN attempt_reviews r ON r.attempt_id=a.id
                WHERE r.attempt_id IS NULL AND a.id IN (
                    SELECT MAX(b.id) FROM attempts b
                    LEFT JOIN attempt_reviews rr ON rr.attempt_id=b.id
                    WHERE rr.attempt_id IS NULL GROUP BY b.va)
                GROUP BY a.status''').fetchall()
            by_status = {row['status']: row['total'] for row in statuses}
            counters = {
                'queue': scalar('SELECT COUNT(*) FROM queue'),
                'matches': by_status.get('MATCH', 0), 'fails': by_status.get('FAIL', 0),
                'deferred': by_status.get('DEFER', 0),
                'void_provider': scalar('SELECT COUNT(*) FROM attempt_reviews'),
                'incidents': scalar('SELECT COUNT(*) FROM provider_incidents'),
                'claims': scalar('SELECT COUNT(*) FROM claims'),
                'inference': scalar('SELECT COUNT(*) FROM inference_slots'),
            }
            if last:
                targets = scalar('SELECT COUNT(*) FROM assignments WHERE run_id=?', run_id)
                claimed = {row['worker_idx']: row['va'] for row in db.execute(
                    'SELECT worker_idx,va FROM claims WHERE run_id=?', (run_id,))}
                # Completed means a terminal result or exhaustion at this run's pass limit.
                completed = scalar('''SELECT COUNT(*) FROM assignments x
                    WHERE x.run_id=? AND (
                      (SELECT t.status FROM attempts t LEFT JOIN attempt_reviews v ON v.attempt_id=t.id
                       WHERE t.va=x.va AND v.attempt_id IS NULL ORDER BY t.id DESC LIMIT 1) IN ('MATCH','SKIP')
                      OR (SELECT COUNT(*) FROM attempts t LEFT JOIN attempt_reviews v ON v.attempt_id=t.id
                          WHERE t.va=x.va AND t.status IN ('FAIL','DEFER') AND v.attempt_id IS NULL) >= ?
                      OR EXISTS (SELECT 1 FROM run_exclusions ex WHERE ex.run_id=x.run_id AND ex.va=x.va)
                    )''', run_id, last['passes'])
                run = {'id': run_id, 'targets': targets, 'completed': completed,
                       'workers': last['workers'], 'finished': bool(last['finished_utc']),
                       'started': last['started_utc']}
            else:
                claimed = {}
                run = {}
            adaptive_row = db.execute('SELECT * FROM adaptive_control WHERE singleton=1').fetchone()
            adaptive = {}
            if adaptive_row and adaptive_row['run_id'] == run_id:
                adaptive = {'concurrency': adaptive_row['concurrency'],
                            'ceiling': adaptive_row['ceiling'],
                            'cooldown_seconds': max(0,int(adaptive_row['cooldown_until']-datetime.now(timezone.utc).timestamp())),
                            'provider_failures': adaptive_row['provider_failures'],
                            'productive_passes': adaptive_row['productive_samples']}
            budget_row = db.execute('SELECT * FROM budget WHERE singleton=1').fetchone()
            budget = {'spend': max(0,budget_row['last_usage'] - budget_row['baseline_usage']) if budget_row else None,
                      'inflight': scalar('SELECT COALESCE(SUM(amount),0) FROM reservations')}
            events = []
            for row in db.execute('''SELECT va,status,utc,worker_idx,id FROM attempts
                    WHERE id NOT IN (SELECT attempt_id FROM attempt_reviews)
                    ORDER BY id DESC LIMIT 45'''):
                events.append({'va': row['va'], 'kind': row['status'], 'time': row['utc'],
                               'worker': None if row['worker_idx'] is None else row['worker_idx']+1,
                               '_sort': row['utc']})
            for row in db.execute('''SELECT va,utc,worker_idx,id FROM provider_incidents
                    ORDER BY id DESC LIMIT 45'''):
                events.append({'va': row['va'], 'kind': 'PROVIDER', 'time': row['utc'],
                               'worker': None if row['worker_idx'] is None else row['worker_idx']+1,
                               '_sort': row['utc']})
            events.sort(key=lambda r:r['_sort'],reverse=True)
            for e in events: e.pop('_sort', None)
            events = events[:50]
    finally:
        db.close()
    workers = []
    if run_id:
        runfile = work/'parallel'/run_id/'run.json'
        try:
            info = json.loads(runfile.read_text())
        except (OSError, ValueError):
            info = {}
        for item in info.get('processes', []):
            i = item.get('worker')
            pid = int(item.get('pid') or 0)
            workers.append({'worker': i+1 if isinstance(i,int) else '?',
                            'pid': pid, 'alive': _process_running(pid), 'current': claimed.get(i)})
    run['live_workers'] = sum(w['alive'] for w in workers)
    return {'generated_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'counters': counters, 'run': run, 'workers': workers,
            'adaptive': adaptive, 'budget': budget, 'events': events}


def serve(repo: Path, port: int = 8765):
    if not 1 <= port <= 65535:
        raise SystemExit('Port must be 1..65535')
    repo = repo.resolve()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            route = urlparse(self.path).path
            if self.headers.get('Host', '').split(':')[0] not in ('localhost', '127.0.0.1'):
                self.send_error(403, 'Localhost only')
                return
            if route == '/':
                content = PAGE.encode('utf-8')
                mime = 'text/html; charset=utf-8'
            elif route == '/api/snapshot':
                try:
                    content = json.dumps(snapshot(repo)).encode('utf-8')
                except (sqlite3.Error, OSError) as exc:
                    content = json.dumps({'error':type(exc).__name__}).encode('utf-8')
                mime = 'application/json; charset=utf-8'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(content)
        def log_message(self, fmt, *args):
            return
    with ThreadingHTTPServer(('127.0.0.1', port), Handler) as http:
        print(f'Dashboard: http://127.0.0.1:{port}', flush=True)
        print('Read-only, listening on localhost only. Press Ctrl+C to stop.', flush=True)
        try:
            http.serve_forever(poll_interval=.5)
        except KeyboardInterrupt:
            pass
    return 0
