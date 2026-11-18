"""Opt-in localhost dashboard controls, reuse validated CLI operations.

Nothing in this module makes direct provider requests or edits Spore source.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
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
        self.launch_settings=None

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

    def _retry_allowlist(self,module):
        """Resolve one explicit allowlist. All-modules dashboard runs fail closed.

        The existing environment override takes priority. When no environment
        override is present, selecting all modules automatically uses the
        user's saved retry-all.json. Never silently expand to the whole queue.
        """
        configured=os.environ.get('SPORE_RETRY_ALLOWLIST','').strip()
        fallback=self.repo/'work/scratch/codex-pass1/retry-all.json'
        if configured:
            path=Path(configured).expanduser().resolve()
            source='SPORE_RETRY_ALLOWLIST'
        elif module=='*':
            path=fallback
            source='retry-all.json'
        else:
            return None
        if not path.is_file():
            raise ValueError(
                'Retry allowlist not found: '+str(path)+'. '
                'All-modules dashboard runs require retry-all.json; '
                'no unrestricted paid run will be started.')
        try:
            data=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError) as exc:
            raise ValueError('Cannot read retry allowlist: '+str(exc)) from exc
        if not isinstance(data,list) or not data:
            raise ValueError('Retry allowlist must be a nonempty JSON array of addresses')
        addresses=set()
        for entry in data:
            if not isinstance(entry,str) or not re.fullmatch(r'0x[0-9a-fA-F]{1,8}',entry):
                raise ValueError('Retry allowlist contains an invalid function address')
            addresses.add(int(entry,16))
        return {'path':str(path),'source':source,'count':len(addresses)}

    def preview(self,module,workers):
        """Read-only current-queue plan. No scope --apply and no model calls.

        Launch applies the chosen scope first, so it can append previously
        undiscovered queue targets. Report the preview as provisional.
        """
        if not isinstance(module,str) or (
                module!='*' and module not in modules(self.repo)):
            raise ValueError('Select a classified module from the list')
        if type(workers) is not int or not 1<=workers<=32:
            raise ValueError('workers must be between 1 and 32')
        selected=self._retry_allowlist(module)
        env=os.environ.copy()
        if selected:
            env['SPORE_RETRY_ALLOWLIST']=selected['path']
        cmd=[sys.executable,str(CLI),'plan','--repo',str(self.repo),
             '--workers',str(workers)]
        cmd+=['--all-modules'] if module=='*' else ['--module',module]
        try:
            result=subprocess.run(cmd,cwd=CLI.parent,stdin=subprocess.DEVNULL,
                                  capture_output=True,text=True,timeout=60,env=env)
        except subprocess.TimeoutExpired:
            raise ValueError('Preview timed out. No agents started.')
        if result.returncode:
            raise ValueError('Preview unavailable: '+
                             (result.stdout+' '+result.stderr)[-300:])
        found=re.search(r'runnable functions \(passes are additional per run\): (\d+)',
                        result.stdout)
        if not found:
            raise ValueError('Preview output could not be interpreted. No agents started.')
        return {'eligible':int(found.group(1)),'module':module,
                'workers':workers,'provisional':True,
                'allowlist_active':bool(selected),
                'allowlist_count':selected['count'] if selected else 0,
                'allowlist_source':selected['source'] if selected else None}

    def _launch_environment(self):
        """Read the current key at launch, not the dashboard's old environment.

        A dashboard can predate an external rotate-key call. Only override its
        inherited environment if the private key file matches the budget row.
        Never launch with a mismatched key or silently reset spending.
        """
        from spore_agentic import KEY_FILE

        env=os.environ.copy()
        dbpath=self.repo/'work/scratch/codex-pass1/spore.sqlite3'
        with sqlite3.connect(dbpath) as db:
            row=db.execute('SELECT key_fingerprint FROM budget WHERE singleton=1').fetchone()
        if row is None or not row[0]:
            return env
        fingerprint=row[0]
        inherited=env.get('OPENROUTER_API_KEY','').strip()
        if inherited and hashlib.sha256(inherited.encode()).hexdigest()[:20]==fingerprint:
            return env
        if KEY_FILE.is_file():
            saved=KEY_FILE.read_text(encoding='utf-8').strip()
            if saved and hashlib.sha256(saved.encode()).hexdigest()[:20]==fingerprint:
                env['OPENROUTER_API_KEY']=saved
                return env
        if inherited:
            raise ValueError(
                'The dashboard inherited an outdated OPENROUTER_API_KEY. '
                'Restart it from the shell with the rotated key, or run '
                'python3 spore_agentic.py rotate-key --save-key and retry.')
        raise ValueError(
            'No saved OpenRouter key matches the spending baseline. '
            'Run python3 spore_agentic.py rotate-key with the replacement key.')

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
            self.process=None  # Do not replay a previous launch failure after a new attempt.
            selected=self._retry_allowlist(module)
            launch_env=self._launch_environment()
            if selected:
                launch_env['SPORE_RETRY_ALLOWLIST']=selected['path']
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
                                              stdout=file,stderr=subprocess.STDOUT,
                                              env=launch_env,start_new_session=True)
            self.launch_settings={
                'module':module,
                'workers':values['workers'],
                'max_inflight':values['max_inflight'],
                'passes':values['passes'],
                'idle_minutes':values['minutes'],
                'budget_usd':values['budget'],
                'allowlist_active':bool(selected),
                'allowlist_count':selected['count'] if selected else 0,
                'allowlist_source':selected['source'] if selected else None
            }
            return {'started':True,'controller_pid':self.process.pid,'module':module}

    def state(self):
        """Report both dashboard-launched and existing local coordinator state.

        A coordinator started from a terminal must also put the dashboard's
        single Launch/Stop control into Stop mode. No log contents or secrets
        are returned, only PID, activity and an exit status where known.
        """
        active=coordinators(self.repo)
        if self.process is None:
            return {'running':bool(active),
                    'pid':active[0] if active else None,
                    'external':bool(active),'returncode':None,
                    'next_launch_allowlist':bool(os.environ.get('SPORE_RETRY_ALLOWLIST')),
                    'allowlist_available':(self.repo/'work/scratch/codex-pass1/retry-all.json').is_file()}
        rc=self.process.poll()
        # A dashboard-owned CLI can have already exited while its spawned
        # coordinator is still active. Keep Stop available until all finish.
        running=rc is None or bool(active)
        info={'pid':self.process.pid if rc is None or not active else active[0],
              'running':running,
              'external':bool(active) and rc is not None,
              'returncode':None if running else rc,
              'next_launch_allowlist':bool(os.environ.get('SPORE_RETRY_ALLOWLIST')),
              'allowlist_available':(self.repo/'work/scratch/codex-pass1/retry-all.json').is_file(),
              'settings':self.launch_settings}
        if not running and rc is not None and rc != 0:
            from spore_dashboard_data import sanitize
            logfile=self.repo/'work/scratch/codex-pass1/dashboard-controller.log'
            try:
                with logfile.open('rb') as f:
                    f.seek(0,2)
                    f.seek(max(0,f.tell()-3600))
                    lines=f.read().decode('utf-8','replace').splitlines()
                error=next((x for x in reversed(lines) if 'Error:' in x or 'failed' in x.lower()),None)
                info['error']=sanitize(error or ('Coordinator exited with status '+str(rc)),350)
            except OSError:
                info['error']='Coordinator exited with status '+str(rc)
        return info

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
