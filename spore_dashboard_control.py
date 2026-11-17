"""Opt-in localhost dashboard controls, reuse validated CLI operations.

Nothing in this module makes direct provider requests or edits Spore source.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading

CLI=Path(__file__).resolve().parent/'spore_agentic.py'
MOD_RE=re.compile(r'[A-Za-z0-9_.+\-]{1,100}\Z')


def modules(repo):
    names=set()
    base=repo/'src/game'
    if base.is_dir():
        names.update(x.name for x in base.iterdir() if x.is_dir() and MOD_RE.fullmatch(x.name))
    path=repo/'work/migrate/modmap.json'
    if path.is_file():
        try:
            obj=json.loads(path.read_text())
            for row in (obj.get('functions') or {}).values():
                name=str(row.get('module') or '').split('/')[0]
                if MOD_RE.fullmatch(name):names.add(name)
        except (OSError,ValueError,AttributeError):
            pass
    return sorted(names)


def coordinators(repo):
    """Only report running coordinator scripts whose Linux cwd is this checkout."""
    found=[]
    root=repo.resolve()
    base=Path('/proc')
    if not base.is_dir():return found
    for p in base.iterdir():
        if not p.name.isdigit():continue
        try:
            raw=(p/'cmdline').read_bytes()
            cmd=[z.decode(errors='replace') for z in raw.split(b'\0') if z]
            if 'run' not in cmd:continue
            if not any(Path(s).name=='spore_parallel_sqlite.py' for s in cmd):continue
            if (p/'cwd').resolve()!=root:continue
            status=(p/'stat').read_text().rsplit(')',1)[1].split()[0]
            if status in ('Z','X'):continue
            found.append(int(p.name))
        except (PermissionError,OSError,ValueError,IndexError):
            continue
    return found


class Controller:
    def __init__(self,repo,enabled=False):
        self.repo=repo.resolve()
        self.enabled=enabled
        self.lock=threading.RLock()
        self.process=None
        self.last_error=''

    def _confirm_idle(self):
        from spore_dashboard_data import connect_ro,proc_running
        if self.process and self.process.poll() is None:
            raise ValueError('A dashboard-launched coordinator is already starting or running')
        running=coordinators(self.repo)
        if running:
            raise ValueError('A decompiler coordinator is already active (PID '+', '.join(map(str,running))+')')
        try:
            with connect_ro(self.repo) as db:
                claims=db.execute('SELECT pid,child_pid FROM claims').fetchall()
                if any(proc_running(r['pid']) or proc_running(r['child_pid']) for r in claims):
                    raise ValueError('Worker or OpenCode child is still active. Stop it before starting another run.')
        except FileNotFoundError:
            raise ValueError('SQLite not initialized. Run setup first.')

    def start(self,data):
        if not self.enabled:raise PermissionError('Controls disabled')
        if not isinstance(data,dict):raise ValueError('Invalid settings')
        module=data.get('module','SPGonzago')
        if module!='*' and (not isinstance(module,str) or module not in modules(self.repo)):
            raise ValueError('Select a classified module from the list')
        values={}
        for field,minimum,maximum in (('workers',1,32),('max_inflight',1,32),('passes',1,24),('minutes',1,120)):
            value=data.get(field)
            if type(value) is not int or not minimum<=value<=maximum:
                raise ValueError(f'{field} must be between {minimum} and {maximum}')
            values[field]=value
        budget=data.get('budget')
        if type(budget) not in (float,int) or not 0<float(budget)<=100000:
            raise ValueError('budget must be positive')
        values['budget']=float(budget)
        with self.lock:
            self._confirm_idle()
            # Scope selection can add functions but never deletes historical work.
            scope=[sys.executable,str(CLI),'scope','--repo',str(self.repo)]
            scope+=['--all-modules'] if module=='*' else ['--module',module]
            scope+=['--apply']
            try:
                result=subprocess.run(scope,cwd=CLI.parent,stdin=subprocess.DEVNULL,
                                      capture_output=True,text=True,timeout=120)
            except subprocess.TimeoutExpired:
                raise ValueError('Module scope calculation timed out. No paid run was started.')
            if result.returncode:
                raise ValueError('Module selection failed: '+(result.stdout+' '+result.stderr)[-700:])
            # CLI owns API-key validation. If it fails, dashboard records failure.
            cmd=[sys.executable,str(CLI),'run','--repo',str(self.repo)]
            cmd+=['--all-modules'] if module=='*' else ['--module',module]
            for name,value in values.items():cmd+=['--'+name.replace('_','-'),str(value)]
            work=self.repo/'work/scratch/codex-pass1'
            work.mkdir(parents=True,exist_ok=True)
            output=work/'dashboard-controller.log'
            with output.open('ab') as file:
                self.process=subprocess.Popen(cmd,cwd=CLI.parent,stdin=subprocess.DEVNULL,
                                              stdout=file,stderr=subprocess.STDOUT,start_new_session=True)
            return {'started':True,'controller_pid':self.process.pid,'module':module}

    def stop(self):
        if not self.enabled:raise PermissionError('Controls disabled')
        with self.lock:
            ids=coordinators(self.repo)
            if ids:
                # Only signal exact coordinator PIDs, never their parent's terminal group.
                for pid in ids:
                    try:os.kill(pid,signal.SIGINT)
                    except ProcessLookupError:pass
                return {'stopping':True,'coordinators':len(ids)}
            if self.process and self.process.poll() is None:
                # CLI may still be starting up, so signal only its owned session.
                try:os.killpg(self.process.pid,signal.SIGINT)
                except ProcessLookupError:pass
                return {'stopping':True,'coordinators':0}
            return {'stopping':False,'message':'No active coordinator found'}
