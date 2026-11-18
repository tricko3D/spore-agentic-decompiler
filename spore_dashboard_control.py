"""Opt-in localhost dashboard controls, reuse validated CLI operations.

Nothing in this module makes direct provider requests or edits Spore source.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
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

    def _allowlist_location(self):
        configured=os.environ.get('SPORE_RETRY_ALLOWLIST','').strip()
        if configured:
            return Path(configured).expanduser().resolve(), 'SPORE_RETRY_ALLOWLIST'
        return self.repo/'work/scratch/codex-pass1/retry-all.json', 'retry-all.json'

    @staticmethod
    def _parse_allowlist(raw):
        try:
            data=json.loads(raw)
        except (ValueError,TypeError) as exc:
            raise ValueError('Invalid JSON: '+str(exc)) from exc
        if not isinstance(data,list) or not data:
            raise ValueError('Allow list must be a nonempty JSON array of function addresses')
        if len(data)>12000:
            raise ValueError('Allow list exceeds 12,000 addresses')
        result=[]
        seen=set()
        for index,item in enumerate(data):
            if not isinstance(item,str) or not re.fullmatch(r'0x[0-9a-fA-F]{1,8}',item):
                raise ValueError('Address '+str(index+1)+' must be 0x followed by 1 to 8 hex digits')
            address='0x'+format(int(item,16),'08x')
            if address in seen:
                raise ValueError('Duplicate function address: '+address)
            seen.add(address)
            result.append(address)
        return result

    def allowlist_document(self):
        """Read only the configured JSON list, never an arbitrary URL/path."""
        path,source=self._allowlist_location()
        with self.lock:
            if not path.is_file():
                return {'path':str(path),'source':source,'exists':False,
                        'revision':'missing','content':'[\n]\n',
                        'count':0,'valid':False,
                        'validation_error':'No list exists. Enter at least one address to create it.'}
            raw=path.read_bytes()
            if len(raw)>256000:
                raise ValueError('Allow list is larger than 256 KB')
            try:
                content=raw.decode('utf-8')
            except UnicodeDecodeError as exc:
                raise ValueError('Allow list must be UTF-8 JSON') from exc
            error=None
            try:
                addresses=self._parse_allowlist(content)
            except ValueError as exc:
                addresses=[]
                error=str(exc)
            return {'path':str(path),'source':source,'exists':True,
                    'revision':hashlib.sha256(raw).hexdigest(),
                    'content':content,
                    'count':len(addresses),'valid':error is None,
                    'validation_error':error}

    def save_allowlist(self,data):
        """Atomically update a checked, nonempty JSON array while idle."""
        if not self.enabled:
            raise PermissionError('Dashboard controls disabled')
        if not isinstance(data,dict) or not isinstance(data.get('content'),str):
            raise ValueError('Expected JSON content for the allow list')
        content=data['content']
        if len(content.encode('utf-8'))>256000:
            raise ValueError('Allow list exceeds 256 KB')
        addresses=self._parse_allowlist(content)
        revision=data.get('revision')
        if not isinstance(revision,str):
            raise ValueError('Missing file revision. Reload the allow list.')
        with self.lock:
            self._confirm_idle()
            path,_=self._allowlist_location()
            previous=path.read_bytes() if path.exists() else None
            if previous is not None and len(previous)>256000:
                raise ValueError('Existing allow list exceeds 256 KB')
            actual=hashlib.sha256(previous).hexdigest() if previous is not None else 'missing'
            if actual!=revision:
                raise ValueError('Allow list changed on disk. Reload it before saving.')
            # A canonical JSON file also makes future manual edits and diffs clear.
            payload=(json.dumps(addresses,indent=2)+'\n').encode('utf-8')
            path.parent.mkdir(parents=True,exist_ok=True)
            fd,tmp=tempfile.mkstemp(prefix='.sad-allowlist-',suffix='.tmp',dir=str(path.parent))
            try:
                with os.fdopen(fd,'wb') as out:
                    out.write(payload)
                    out.flush()
                    os.fsync(out.fileno())
                if previous is not None:
                    os.chmod(tmp,path.stat().st_mode & 0o777)
                os.replace(tmp,path)
                try:
                    dirfd=os.open(str(path.parent),os.O_RDONLY)
                    try:os.fsync(dirfd)
                    finally:os.close(dirfd)
                except OSError:
                    pass
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            return self.allowlist_document()

    def _retry_allowlist(self,module):
        """All-modules runs use a validated list and never fall back to the queue."""
        path,source=self._allowlist_location()
        configured=bool(os.environ.get('SPORE_RETRY_ALLOWLIST','').strip())
        if module not in ('*','__allowlist__') and not configured:
            return None
        if not path.is_file():
            raise ValueError('Retry allow list not found: '+str(path)+
                             '. All-modules launches require a saved nonempty list.')
        raw=path.read_bytes()
        if len(raw)>256000:
            raise ValueError('Retry allow list exceeds 256 KB')
        try:
            addresses=self._parse_allowlist(raw.decode('utf-8'))
        except UnicodeDecodeError as exc:
            raise ValueError('Retry allow list must be UTF-8 JSON') from exc
        return {'path':str(path),'source':source,'count':len(addresses)}

    def _allowlist_breakdown_fallback(self, env):
        """Read-only reconciliation against the currently INSTALLED local runner.

        This works without reinstalling the bundled runner following git pull.
        It reads the identical SQLite queue, valid attempts and module/manifest
        classifier as make_assignments, without calling inference providers.
        """
        script = r'''
import json, os
from pathlib import Path
import spore_parallel_sqlite as runner
module=runner.load_runner()
selected={f'0x{int(va,16):08x}' for va in json.loads(
    Path(os.environ['SPORE_RETRY_ALLOWLIST']).read_text(encoding='utf-8'))}
queue=runner.read_queue()
queued={row['va'] for row in queue}
classifier=module.ModuleIndex()
classifier.refresh()
classified={va for va in queued if classifier.eligible(va)}
manifested=module.ManifestIndex().refresh()
latest={row['va']:row['status'] for row in runner.store.read_attempts()}
reasons={k:[] for k in (
    'not_in_queue','outside_classified_scope','already_manifested',
    'latest_match','latest_skip','other')}
for va in sorted(selected):
    if va not in queued: reasons['not_in_queue'].append(va)
    elif va not in classified: reasons['outside_classified_scope'].append(va)
    elif va in manifested: reasons['already_manifested'].append(va)
    elif latest.get(va)=='MATCH': reasons['latest_match'].append(va)
    elif latest.get(va)=='SKIP': reasons['latest_skip'].append(va)
eligible=len(selected)-sum(map(len,reasons.values()))
print(json.dumps({
    'listed':len(selected),'eligible':eligible,
    'excluded':len(selected)-eligible,
    'reasons':{k:len(v) for k,v in reasons.items()},
    'addresses':reasons},separators=(',',':')))
'''
        try:
            proc=subprocess.run([sys.executable,'-c',script],
                cwd=self.repo,env=env,stdin=subprocess.DEVNULL,
                capture_output=True,text=True,timeout=60)
            if proc.returncode != 0:
                return None
            return json.loads(proc.stdout)
        except (OSError,ValueError,subprocess.TimeoutExpired):
            return None

    def preview(self,module,workers):
        """Read-only current-queue plan. No scope --apply and no model calls.

        Launch applies the chosen scope first, so it can append previously
        undiscovered queue targets. Report the preview as provisional.
        """
        if not isinstance(module,str) or (
                module not in ('*','__allowlist__') and module not in modules(self.repo)):
            raise ValueError('Select a classified module or the allow list')
        if type(workers) is not int or not 1<=workers<=32:
            raise ValueError('workers must be between 1 and 32')
        selected=self._retry_allowlist(module)
        env=os.environ.copy()
        if selected:
            env['SPORE_RETRY_ALLOWLIST']=selected['path']
            env['SPORE_ALLOWLIST_BREAKDOWN']='1'
            # Mirror the CLI module scope for the local diagnostic fallback.
            from spore_agentic import config as scope_config
            saved=scope_config()
            env['SPORE_TARGET_MODULES']='*' if module in ('*','__allowlist__') else module
            env['SPORE_MIN_MODULE_CONFIDENCE']=str(saved.get('min_confidence',0.70))
        cmd=[sys.executable,str(CLI),'plan','--repo',str(self.repo),
             '--workers',str(workers)]
        cmd+=['--all-modules'] if module in ('*','__allowlist__') else ['--module',module]
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
        eligible=int(found.group(1))
        breakdown=None
        if selected:
            match=re.search(r'^SPORE_ALLOWLIST_BREAKDOWN=(\{[^\n]*\})$',
                            result.stdout,re.MULTILINE)
            if match:
                try:breakdown=json.loads(match.group(1))
                except ValueError:pass
            if not breakdown:
                breakdown=self._allowlist_breakdown_fallback(env)
            # If the reconciliation disagrees with the authoritative plan,
            # do not show potentially stale or misleading category counts.
            if breakdown and (
                    breakdown.get('eligible')!=eligible
                    or breakdown.get('listed')!=selected['count']):
                breakdown=None
        return {'eligible':eligible,'module':module,
                'workers':workers,'provisional':True,
                'allowlist_active':bool(selected),
                'allowlist_count':selected['count'] if selected else 0,
                'allowlist_source':selected['source'] if selected else None,
                'allowlist_breakdown':breakdown}

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
        if not isinstance(module,str) or (
                module not in ('*','__allowlist__') and module not in modules(self.repo)):
            raise ValueError('Select a classified module or the allow list')
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
            scope+=['--all-modules'] if module in ('*','__allowlist__') else ['--module',module]
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
            cmd+=['--all-modules'] if module in ('*','__allowlist__') else ['--module',module]
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
