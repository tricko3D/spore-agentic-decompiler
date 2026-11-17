#!/usr/bin/env python3
"""Launch configurable N SPGonzago DeepSeek workers using SQLite WAL state.

Shared state lives in spore.sqlite3; preexisting CSV files are read-only backups.
The workers share a working tree (this is not OS filesystem isolation).
"""
import argparse
from datetime import datetime, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent / "work" / "scratch"))
import spore_sqlite_store as store
import time

ROOT = Path(__file__).resolve().parent
WORK = ROOT / 'work' / 'scratch' / 'codex-pass1'
RUNNER = ROOT / 'work' / 'scratch' / 'spore_first_pass_sqlite.py'
RUN_LOGS = WORK / 'parallel'
LOCK = WORK / 'sqlite-parallel.lock'
LATEST = RUN_LOGS / 'latest.json'
KNOWN_CHECK_MARKER = 'SPGONZAGO_SQLITE_CHECK_LOCK_V1'


def load_runner():
    if not RUNNER.is_file():
        sys.exit('Parallel runner not installed. Unzip the package into your repository root.')
    spec = importlib.util.spec_from_file_location('spore_parallel_worker_runner', RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if mod.PARALLEL_RUNNER_VERSION != 'SPGONZAGO_SQLITE_V1':
        sys.exit('Unexpected parallel runner version. Refusing to start.')
    return mod


def verify_checker():
    path = ROOT / 'tools' / 'contrib' / 'check.py'
    if not path.is_file() or KNOWN_CHECK_MARKER not in path.read_text(encoding='utf-8'):
        sys.exit('Shared checker lock not installed. Run: .venv/bin/python install_spore_sqlite.py')


def other_runners():
    """Fail closed before creating workers when any legacy/new runner is active."""
    found=[]
    procroot=Path('/proc')
    if not procroot.is_dir():
        raise RuntimeError('Linux /proc is required to detect legacy writers')
    for p in procroot.iterdir():
        if not p.name.isdigit() or int(p.name)==os.getpid():
            continue
        try:
            argv=[x.decode('utf-8','replace') for x in (p/'cmdline').read_bytes().split(b'\0') if x]
        except (OSError,PermissionError):
            continue
        if 'run' not in argv:
            continue
        names=('spore_first_pass.py','spore_first_pass_parallel.py','spore_first_pass_sqlite.py')
        if any(any(a == n or a.endswith('/'+n) for n in names) for a in argv):
            found.append((p.name,' '.join(argv)[:180]))
    return found


def read_queue():
    return store.read_queue()


def source_groups(allowed):
    """Group targets whose known C++ is in the same file, avoiding edit races.

    Reading source markers is conservative. An agent still has broad OS-level
    write permission, and must obey its target-only prompt.
    """
    marker = re.compile(r'(?m)^\s*//\s*@\s*0x0*([0-9a-fA-F]{6,8})\b')
    allowed_int = {int(va, 16): va for va in allowed}
    file_for_va = {}
    candidates = []
    for d in (ROOT / 'src' / 'game', ROOT / 'match' / 'slices'):
        if d.is_dir():
            candidates.extend(p for p in d.rglob('*') if p.is_file() and p.suffix.lower() in ('.cpp', '.cc', '.cxx', '.c'))
    for path in candidates:
        try:
            content = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        for match in marker.finditer(content):
            va = allowed_int.get(int(match.group(1), 16))
            if va:
                # Prefer migrated src/game over a shadow slice when both exist.
                if va not in file_for_va or 'src/game/' in str(path):
                    file_for_va[va] = str(path.relative_to(ROOT))
    groups = {}
    for va in allowed:
        key = file_for_va.get(va, 'unique:' + va)
        groups.setdefault(key, []).append(va)
    return list(groups.values())


def make_assignments(queue, workers, mod):
    m = mod.ModuleIndex()
    m.refresh()
    allowed = [r['va'] for r in queue if m.eligible(r['va'])]
    if not allowed:
        sys.exit('No SPGonzago functions passed the module confidence filter. No agents started.')
    groups = source_groups(allowed)
    position = {va: i for i, va in enumerate(allowed)}
    groups.sort(key=lambda g: min(position[va] for va in g))
    counts = [0] * workers
    assignments = {}
    for group in groups:
        index = min(range(workers), key=lambda i: (counts[i], i))
        for va in group:
            assignments[va] = index
        counts[index] += len(group)
    assert len(assignments) == len(allowed)
    return allowed, assignments, counts


def status():
    if not LATEST.is_file():
        print('No parallel run recorded yet.')
        return
    r = json.loads(LATEST.read_text(encoding='utf-8'))
    print('Parallel batch:', r['run_id'])
    print('Settings:', r['workers'], 'workers,', r['passes'], 'total passes,', r['minutes'], 'minutes/pass,',
          r.get('max_inflight','legacy'), 'max simultaneous inferences')
    print('Run directory:', r['directory'])
    for w in r['processes']:
        pid = w['pid']
        try:
            os.kill(pid, 0)
            state = 'process exists'
        except ProcessLookupError:
            state = 'not running'
        except PermissionError:
            state = 'running (not accessible)'
        log = Path(w['log'])
        tail = ''
        if log.is_file():
            with log.open(encoding='utf-8', errors='replace') as f:
                for line in f:
                    if line.strip():
                        tail = line.strip()
        print(f"  Worker {w['worker'] + 1}/{r['workers']}: PID {pid}, {state}, targets {w['assigned']}, log: {log}")
        if tail:
            print('    ', tail[:230])
    print('\nOverall status:')
    p = subprocess.run([sys.executable, str(RUNNER), 'status'], cwd=ROOT)
    return p.returncode


def run(a):
    if Path.cwd().resolve() != ROOT:
        sys.exit(f'Run from {ROOT}')
    if not (1 <= a.workers <= 32):
        sys.exit('--workers must be between 1 and 32')
    if not (1 <= a.passes <= 24):
        sys.exit('--passes must be between 1 and 24')
    if a.minutes < 1 or a.budget <= 0 or a.max_functions_per_worker < 0 or a.quota_wait < 0 or a.max_inflight < 0 or a.provider_retries < 0:
        sys.exit('Invalid time, budget, quota wait or function limit')
    if shutil.which('opencode') is None:
        sys.exit('opencode not found on PATH')
    if not os.environ.get('OPENROUTER_API_KEY', '').strip():
        sys.exit('OPENROUTER_API_KEY not set in this shell')
    mod = load_runner()
    verify_checker()
    RUN_LOGS.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    with LOCK.open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit('Another parallel coordinator is running. Open a second shell and use: .venv/bin/python spore_parallel_sqlite.py status')
        store.ensure_imported()
        store.assert_legacy_unchanged()
        others = other_runners()
        if others:
            details = '\n'.join('  PID ' + pid + ': ' + cmd for pid, cmd in others[:6])
            sys.exit('Stop existing runner processes before starting SQLite workers:\n' + details)
        store.clear_stale_claims()
        store.clear_stale_reservations()
        store.clear_stale_inference_slots()
        queue = read_queue()
        allowed, assignments, counts = make_assignments(queue, a.workers, mod)
        run_id = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + f'-{os.getpid()}'
        folder = RUN_LOGS / run_id
        folder.mkdir(parents=True, exist_ok=False)
        store.create_run(run_id,a.workers,a.passes,assignments)
        print(f'SPGonzago: {len(allowed)}/{len(queue)} classified targets. '
              f'Workers: {a.workers}; assignment sizes: {counts}', flush=True)
        print(f'Shared SQLite state: {store.DB} (WAL transactions)', flush=True)
        print(f'Logs: {folder}', flush=True)
        print('Original legacy CSV and runner are unchanged.', flush=True)
        processes = []
        live = []
        for i in range(a.workers):
            log_path = folder / f'worker-{i+1:02d}.log'
            cmd = [sys.executable, str(RUNNER), 'run', '--passes', str(a.passes),
                   '--reasoning', a.reasoning, '--minutes', str(a.minutes),
                   '--quota-wait', str(a.quota_wait), '--budget', str(a.budget),
                   '--max-inflight', str(a.max_inflight),
                   '--provider-retries', str(a.provider_retries),
                   '--run-id', run_id, '--worker-idx', str(i)]
            if a.max_functions_per_worker:
                cmd += ['--max-functions', str(a.max_functions_per_worker)]
            if a.unsafe:
                cmd += ['--unsafe']
            env = os.environ.copy()
            env.update({'PWD': str(ROOT)})
            with log_path.open('w', encoding='utf-8') as output:
                p = subprocess.Popen(cmd, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                     stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            live.append(p)
            processes.append({'worker': i, 'pid': p.pid, 'assigned': counts[i], 'log': str(log_path)})
            print(f'  Worker {i+1}/{a.workers}: PID {p.pid}, assigned {counts[i]}, log {log_path.name}', flush=True)
        report = {'run_id': run_id, 'directory': str(folder), 'workers': a.workers,
                  'passes': a.passes, 'minutes': a.minutes, 'max_inflight': a.max_inflight,
                  'provider_retries': a.provider_retries, 'processes': processes}
        (folder / 'run.json').write_text(json.dumps(report, indent=2) + '\n')
        tmp = LATEST.with_name(f'latest.{os.getpid()}.tmp')
        tmp.write_text(json.dumps(report, indent=2) + '\n')
        os.replace(tmp, LATEST)
        print('\nLive overview in another shell: .venv/bin/python spore_parallel_sqlite.py status', flush=True)
        print('Press Ctrl+C to stop all workers cleanly. No result history is reset.', flush=True)
        done = set()
        interrupted = False
        try:
            while len(done) < len(live):
                for i, proc in enumerate(live):
                    if i in done:
                        continue
                    rc = proc.poll()
                    if rc is not None:
                        done.add(i)
                        print(f'  Worker {i+1} exited with status {rc}. Log: {processes[i]["log"]}', flush=True)
                if len(done) < len(live):
                    time.sleep(2)
        except KeyboardInterrupt:
            interrupted = True
            print('\nStopping workers...', flush=True)
            for proc in live:
                if proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGINT)
                    except ProcessLookupError:
                        pass
            for proc in live:
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()
        store.finish_run(run_id)
        print('\nFinal shared status:', flush=True)
        subprocess.run([sys.executable, str(RUNNER), 'status'], cwd=ROOT)
        return 130 if interrupted else (0 if all(p.returncode == 0 for p in live) else 1)


def main():
    p = argparse.ArgumentParser(description='Run configurable parallel DeepSeek agents on SPGonzago only')
    sub = p.add_subparsers(dest='command', required=True)
    r = sub.add_parser('run', help='Start N workers using SQLite WAL')
    r.add_argument('--workers', type=int, default=5, help='Number of concurrent agents (default: 5)')
    r.add_argument('--passes', type=int, default=3, help='Total attempts allowed for each function (1-24)')
    r.add_argument('--reasoning', choices=['low', 'medium', 'high'], default='medium')
    r.add_argument('--minutes', type=int, default=5, help='Minutes for each agent pass')
    r.add_argument('--quota-wait', type=int, default=1, help='Minutes to wait on API rate limit; 0 stops that worker')
    r.add_argument('--max-inflight', type=int, default=8, help='Concurrent OpenRouter inference cap, independent of worker count')
    r.add_argument('--provider-retries', type=int, default=2, help='Additional provider-only retries before worker stops, without ticking a pass')
    r.add_argument('--budget', type=float, default=20.0, help='Cumulative shared OpenRouter soft limit, USD')
    r.add_argument('--unsafe', action='store_true', help='Existing runner flag; OpenCode remains unsandboxed')
    r.add_argument('--max-functions-per-worker', type=int, default=0, help='Optional limit for initial small-scale test')
    plan=sub.add_parser('plan', help='Preview SPGonzago split without spending any tokens')
    plan.add_argument('--workers',type=int,default=5)
    sub.add_parser('status', help='Show each worker and aggregate SQLite progress')
    a = p.parse_args()
    if a.command == 'run':
        return run(a)
    if a.command == 'plan':
        store.ensure_imported()
        store.assert_legacy_unchanged()
        if not 1<=a.workers<=32:
            p.error('--workers must be between 1 and 32')
        mod=load_runner()
        allowed,assignments,counts=make_assignments(read_queue(),a.workers,mod)
        print(f'SPGonzago eligible scope: {len(allowed)} functions')
        print('Assignments:',counts)
        print('No agents started; SQLite history unchanged.')
        return 0
    return status()


if __name__ == '__main__':
    sys.exit(main())
