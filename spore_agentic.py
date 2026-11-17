#!/usr/bin/env python3
"""Standalone controller for byte-exact Spore decompilation inside an upstream checkout.

Requires Python 3.10+ on Linux or WSL. No third-party Python dependencies.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import hashlib
import math
import json
import re
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
import urllib.request
import urllib.error

HERE = Path(__file__).resolve().parent
ENGINE = HERE / 'engine'
CONFIG_HOME = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'spore-agentic-decompiler'
CONFIG_FILE = CONFIG_HOME / 'config.json'
KEY_FILE = CONFIG_HOME / 'openrouter.key'
STAMP = lambda: datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
# This is intentionally an explicit allowlist, not a recursive checkout copy.
INSTALL = {
    'spore_parallel_sqlite.py': 'spore_provider_fix/spore_parallel_sqlite.py',
    'spore_matched_bytes.py': 'spore_provider_fix/spore_matched_bytes.py',
    'spore_recover_provider_attempts.py': 'spore_provider_fix/spore_recover_provider_attempts.py',
    'work/scratch/spore_first_pass_sqlite.py': 'spore_provider_fix/work/scratch/spore_first_pass_sqlite.py',
    'work/scratch/spore_sqlite_store.py': 'spore_provider_fix/work/scratch/spore_sqlite_store.py',
    'work/scratch/spore_provider_log.py': 'spore_provider_fix/work/scratch/spore_provider_log.py',
    'work/scratch/spore_adaptive.py': 'spore_provider_fix/work/scratch/spore_adaptive.py',
    'work/scratch/spore_progress_watch.py': 'spore_provider_fix/work/scratch/spore_progress_watch.py',
    'work/scratch/spore_first_pass_prompt.txt': 'spore_first_pass_prompt.txt',
    'migrate_spore_sqlite.py': 'migrate_spore_sqlite.py',
    'install_spore_sqlite.py': 'install_spore_sqlite.py',
    'bootstrap_spore_queue.py': 'bootstrap_queue.py',
    'extend_spore_queue.py': 'expand_queue.py',
}


def fail(message):
    raise SystemExit(f'Error: {message}')


def config():
    try:
        obj = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
        if not isinstance(obj, dict):
            fail(f'Invalid configuration: {CONFIG_FILE}')
        return obj
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        fail(f'Cannot read {CONFIG_FILE}: {exc}')


def atomic_private_file(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            os.fchmod(out.fileno(), 0o600)
            out.write(contents)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def repo_path(args, saved=None):
    raw = getattr(args, 'repo', None) or os.environ.get('SPORE_DECOMP_REPO') or (saved or config()).get('repo')
    if not raw:
        if sys.stdin.isatty():
            raw = input('Path to your existing spore-decomp checkout [~/spore-decomp]: ').strip() or '~/spore-decomp'
        else:
            fail('Missing repository path. Supply --repo /path/to/spore-decomp or run setup interactively.')
    path = Path(raw).expanduser().resolve()
    if not (path / 'tools/contrib/task.py').is_file() or not (path / 'tools/contrib/check.py').is_file():
        fail(f'{path} does not look like spore-decomp (missing tools/contrib/task.py or check.py)')
    return path


def python_for(repo):
    venv = repo / '.venv/bin/python'
    return str(venv if venv.is_file() else Path(sys.executable).resolve())


def execute(repo, *argv, env=None, check=True):
    cmd = [python_for(repo), *map(str, argv)]
    print('$', ' '.join(cmd[:5]) + (' ...' if len(cmd)>5 else ''), flush=True)
    return subprocess.run(cmd, cwd=repo, env=env, check=check).returncode


def live_workers():
    proc = Path('/proc')
    if not proc.is_dir():
        fail('Linux/WSL /proc is required for safe worker detection')
    found = []
    for p in proc.iterdir():
        if not p.name.isdigit() or int(p.name) == os.getpid():
            continue
        try:
            argv = [x.decode('utf-8', 'replace') for x in
                    (p/'cmdline').read_bytes().split(b'\x00') if x]
        except (PermissionError, OSError):
            continue
        scripts = ('spore_first_pass_sqlite.py', 'spore_parallel_sqlite.py')
        if 'run' in argv and any(Path(arg).name in scripts for arg in argv):
            found.append(p.name)
    return found


def ensure_stopped():
    if live_workers():
        fail('Active Spore workers detected (PIDs: ' + ', '.join(live_workers()) + '). Stop them before installing or recovering.')


def install(repo):
    ensure_stopped()
    missing = [p for p in INSTALL.values() if not (ENGINE/p).is_file()]
    if missing:
        fail('Engine files missing from application installation: '+', '.join(missing))
    # A user-edited instruction prompt is part of their workflow, not a disposable engine binary.
    changes = [(rel, ENGINE/src) for rel, src in INSTALL.items()
               if (rel != 'work/scratch/spore_first_pass_prompt.txt' or not (repo/rel).exists())
               and (not (repo/rel).is_file() or (repo/rel).read_bytes() != (ENGINE/src).read_bytes())]
    if not changes:
        print('Engine scripts already up to date.')
        return
    backup = repo/'work/scratch/codex-pass1/sqlite-backups'/f'agentic-scripts-{STAMP()}'
    backup.mkdir(parents=True, exist_ok=False)
    for rel, source in changes:
        target = repo/rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            previous = backup/rel
            previous.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, previous)
        staged = target.with_name(f'.{target.name}.{os.getpid()}.tmp')
        try:
            shutil.copy2(source, staged)
            os.replace(staged, target)
        finally:
            staged.unlink(missing_ok=True)
        print('Installed', rel)
    print('Backups of replaced scripts:', backup)


def get_key(prompt=False):
    env = os.environ.get('OPENROUTER_API_KEY', '').strip()
    if not prompt and env:
        return env
    if not prompt and KEY_FILE.is_file():
        return KEY_FILE.read_text(encoding='utf-8').strip()
    if not sys.stdin.isatty():
        fail('OPENROUTER_API_KEY not set. Run setup interactively or export it in your shell.')
    key = getpass.getpass('OpenRouter API key (input hidden, stored locally with permission 0600): ').strip()
    if not key:
        fail('An API key is required')
    if not key.startswith('sk-or-'):
        fail('Unexpected OpenRouter key format (expected sk-or-...)')
    atomic_private_file(KEY_FILE, key+'\n')
    return key


def sync_saved_key(key, save_new=False):
    """Keep an existing private key file in sync after rotation.

    Environment-only credentials remain environment-only unless --save-key
    explicitly opts into persisting them for a running dashboard.
    """
    if save_new or KEY_FILE.is_file():
        if not KEY_FILE.is_file() or KEY_FILE.read_text(encoding='utf-8').strip() != key:
            atomic_private_file(KEY_FILE, key+'\n')
            print('Saved OpenRouter key updated for dashboard launches.')


def scope_settings(args, previous):
    module_arg = getattr(args, 'module', None)
    all_modules = getattr(args, 'all_modules', False)
    if all_modules and module_arg:
        fail('--all-modules cannot be combined with --module')
    if all_modules:
        modules = ['*']
    elif module_arg:
        modules = list(dict.fromkeys(module_arg))
    else:
        modules = previous.get('modules', ['SPGonzago'])
    if not isinstance(modules, list) or not modules:
        fail('Select at least one module')
    if '*' in modules and len(modules) != 1:
        fail('All modules must be selected using --all-modules')
    for module in modules:
        if module != '*' and (not isinstance(module, str) or not re.fullmatch(r'[A-Za-z0-9_.+\-]+', module)):
            fail(f'Invalid module name: {module!r}')
    minimum = getattr(args, 'min_confidence', None)
    if minimum is None:
        minimum = previous.get('min_confidence', 0.70)
    if not 0.0 <= float(minimum) <= 1.0:
        fail('--min-confidence must be between 0 and 1')
    return {'modules': modules, 'min_confidence': float(minimum)}


def with_scope(env, settings):
    return {**env, 'SPORE_TARGET_MODULES': ';'.join(settings['modules']),
            'SPORE_MIN_MODULE_CONFIDENCE': str(settings['min_confidence'])}


def add_scope_arguments(parser):
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--module', action='append', metavar='NAME',
                       help='Select a module; repeat to select several (default: saved scope)')
    group.add_argument('--all-modules', action='store_true',
                       help='Use all modules with confidence >= threshold')
    parser.add_argument('--min-confidence', type=float,
                        help='Minimum module mapping confidence, default 0.70')


def command_setup(args):
    if sys.platform != 'linux':
        fail('This runner requires Linux or Windows Subsystem for Linux (WSL)')
    previous = config()
    repo = repo_path(args, previous)
    print('Using upstream checkout:', repo)
    ensure_stopped()
    if not shutil.which('opencode'):
        fail('OpenCode CLI not found. Install OpenCode and ensure `opencode` works in WSL before setup.')
    # Preserve an existing key without prompting, unless the user explicitly rotates it.
    get_key(prompt=args.change_key or not (os.environ.get('OPENROUTER_API_KEY') or KEY_FILE.is_file()))
    settings = {
        'repo': str(repo),
        'workers': args.workers if args.workers is not None else int(previous.get('workers', 5)),
        'max_inflight': args.max_inflight if args.max_inflight is not None else int(previous.get('max_inflight', 4)),
        'passes': args.passes if args.passes is not None else int(previous.get('passes', 3)),
        'minutes': args.minutes if args.minutes is not None else int(previous.get('minutes', 20)),
        'budget': args.budget if args.budget is not None else float(previous.get('budget', 20.0)),
        'quota_wait': int(previous.get('quota_wait', 1)),
        'provider_retries': int(previous.get('provider_retries', 2)),
        'reasoning': previous.get('reasoning', 'high'),
        **scope_settings(args, previous),
    }
    validate_settings(settings)
    install(repo)
    work = repo/'work/scratch/codex-pass1'
    if not (work/'spore.sqlite3').exists():
        if not (work/'queue.csv').exists() and not (work/'results.csv').exists():
            execute(repo, 'bootstrap_spore_queue.py', env=with_scope(os.environ.copy(), settings))
        elif not (work/'queue.csv').exists() or not (work/'results.csv').exists():
            fail('Only one of queue.csv/results.csv exists. Refusing to invent lost history.')
        execute(repo, 'migrate_spore_sqlite.py')
    else:
        print('Existing SQLite database found. Its history will be preserved.')
    execute(repo, 'install_spore_sqlite.py')
    atomic_private_file(CONFIG_FILE, json.dumps(settings, indent=2)+'\n')
    # Never echo the API key or persist it in the spore-decomp checkout.
    print('Setup complete. Config:', CONFIG_FILE)
    print('OpenRouter key:', 'environment' if os.environ.get('OPENROUTER_API_KEY') else KEY_FILE)
    print('Run `python3 spore_agentic.py recover` to inspect historic provider-only failures.')
    print('Run `python3 spore_agentic.py plan` before your first paid batch.')


def verify_new_openrouter_key(key):
    """Validate a replacement API key without purchasing model inference."""
    req = urllib.request.Request(
        'https://openrouter.ai/api/v1/key',
        headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        fail(f'New OpenRouter key validation failed (HTTP {exc.code}); SQLite was not changed.')
    except (OSError, ValueError, TimeoutError) as exc:
        fail(f'Could not validate the replacement key ({type(exc).__name__}); SQLite was not changed.')
    data = result.get('data') if isinstance(result, dict) else None
    if not isinstance(data, dict) or isinstance(data.get('usage'), bool) or not isinstance(data.get('usage'), (int, float)):
        fail('OpenRouter returned no numeric usage for the new key; SQLite was not changed.')
    usage = float(data['usage'])
    if not math.isfinite(usage) or usage < 0:
        fail('OpenRouter returned invalid usage for the replacement key; SQLite was not changed.')
    remaining = data.get('limit_remaining')
    if isinstance(remaining, (int, float)) and not isinstance(remaining, bool) and float(remaining) <= 0:
        fail('The replacement key has no remaining spending allowance; SQLite was not changed.')
    return usage


def rebind_budget_key(repo, fingerprint, current_usage):
    """Bind spending history to a new key without losing prior costs or attempts.

    Project spend equals (new key current usage - new baseline). Keeping the
    same cumulative spend means the new baseline may be negative, which is
    mathematically correct and not a reset of the user's tracked spending.
    """
    dbpath = repo/'work/scratch/codex-pass1/spore.sqlite3'
    if not dbpath.is_file():
        fail('No SQLite database found. Initialize the selected checkout with setup first.')
    with sqlite3.connect(dbpath, timeout=60) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=60000')
        row = db.execute('SELECT * FROM budget WHERE singleton=1').fetchone()
        if row is None:
            fail('No existing spending record was found. Refusing to guess the budget baseline.')
        if row['key_fingerprint'] == fingerprint:
            return {'changed': False, 'spent': float(row['last_usage'])-float(row['baseline_usage']), 'backup': None}
        spent = float(row['last_usage']) - float(row['baseline_usage'])
        if not math.isfinite(spent) or spent < 0:
            fail('The stored spending history is invalid; refusing key rotation.')
        backup_dir = dbpath.parent/'sqlite-backups'
        backup_dir.mkdir(parents=True,exist_ok=True)
        backup = backup_dir / ('spore-before-key-rotation-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.sqlite3')
        with sqlite3.connect(backup) as dest:
            db.backup(dest)
        db.execute('BEGIN IMMEDIATE')
        try:
            live = db.execute('SELECT * FROM budget WHERE singleton=1').fetchone()
            if (live['key_fingerprint'] != row['key_fingerprint'] or
                live['last_usage'] != row['last_usage'] or
                live['baseline_usage'] != row['baseline_usage']):
                raise RuntimeError('Spending record changed during rotation; database not modified')
            db.execute("""CREATE TABLE IF NOT EXISTS budget_key_rotations (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          old_fingerprint TEXT NOT NULL,new_fingerprint TEXT NOT NULL,
                          carried_spend REAL NOT NULL,new_key_usage REAL NOT NULL,
                          rotated_utc TEXT NOT NULL)""")
            db.execute("""UPDATE budget SET key_fingerprint=?, baseline_usage=?,
                          last_usage=?, updated_utc=? WHERE singleton=1""",
                       (fingerprint, float(current_usage)-spent, float(current_usage),
                        datetime.now(timezone.utc).isoformat()))
            db.execute("""INSERT INTO budget_key_rotations
                          (old_fingerprint,new_fingerprint,carried_spend,new_key_usage,rotated_utc)
                          VALUES(?,?,?,?,?)""",
                       (row['key_fingerprint'], fingerprint, spent, current_usage,
                        datetime.now(timezone.utc).isoformat()))
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {'changed': True, 'spent': spent, 'backup': backup}


def command_rotate_key(args):
    repo = repo_path(args)
    ensure_stopped()
    key = get_key()
    fingerprint = hashlib.sha256(key.encode()).hexdigest()[:20]
    dbpath = repo/'work/scratch/codex-pass1/spore.sqlite3'
    if not dbpath.is_file():
        fail('SQLite database not initialized. Run setup first.')
    with sqlite3.connect(dbpath) as db:
        row = db.execute('SELECT key_fingerprint FROM budget WHERE singleton=1').fetchone()
    if row is None:
        fail('No existing spending record. Refusing to guess history.')
    if row[0] == fingerprint:
        sync_saved_key(key, save_new=args.save_key)
        print('OpenRouter key already matches the stored budget identity. No changes needed.')
        return 0
    print('Validating the replacement key with OpenRouter (no inference charges)...', flush=True)
    usage = verify_new_openrouter_key(key)
    result = rebind_budget_key(repo, fingerprint, usage)
    sync_saved_key(key, save_new=args.save_key)
    print(f'New key accepted; preserved ${result["spent"]:.4f} of cumulative tracked spending.')
    print('SQLite backup:', result['backup'])
    print('Verified matches, retry allowances, attempts and source files were not modified.')
    return 0


def validate_settings(s):
    scope_settings(argparse.Namespace(), s)
    if not 1 <= int(s['workers']) <= 32:
        fail('workers must be 1..32')
    if not 1 <= int(s['max_inflight']) <= 32:
        fail('max-inflight must be 1..32')
    if not 1 <= int(s['passes']) <= 24:
        fail('passes must be 1..24')
    if not 1 <= int(s['minutes']) <= 120:
        fail('minutes must be 1..120')
    if not 0 < float(s['budget']) <= 100000:
        fail('budget must be positive')


def command_run(args, mode):
    saved = config()
    repo = repo_path(args, saved)
    if not (repo/'work/scratch/codex-pass1/spore.sqlite3').is_file():
        fail('SQLite state not initialized. Run setup first.')
    if mode in ('scope', 'modules'):
        requested = scope_settings(args, saved)
        env = with_scope(os.environ.copy(), requested)
        argv = ['extend_spore_queue.py']
        if mode == 'modules':
            argv.append('--list-modules')
        if mode == 'scope' and args.apply:
            ensure_stopped()
            argv.append('--apply')
        rc = execute(repo, *argv, env=env, check=False)
        if rc == 0 and mode == 'scope' and args.apply:
            atomic_private_file(CONFIG_FILE, json.dumps({**saved, **requested}, indent=2)+'\n')
            print('Saved default module scope:', ', '.join(requested['modules']))
        return rc
    if mode == 'run':
        settings = {**saved}
        for name in ('workers', 'max_inflight', 'passes', 'minutes', 'budget'):
            value = getattr(args, name, None)
            if value is not None:
                settings[name] = value
        settings.update(scope_settings(args, saved))
        validate_settings(settings)
        env = with_scope(os.environ.copy(), settings)
        env['OPENROUTER_API_KEY'] = get_key()
        expected_key_fingerprint = hashlib.sha256(env['OPENROUTER_API_KEY'].encode()).hexdigest()[:20]
        with sqlite3.connect(repo/'work/scratch/codex-pass1/spore.sqlite3') as budget_db:
            stored = budget_db.execute('SELECT key_fingerprint FROM budget WHERE singleton=1').fetchone()
        if stored and stored[0] != expected_key_fingerprint:
            fail('OpenRouter key changed since the stored spending baseline. '
                 'Run `python3 spore_agentic.py rotate-key` to safely preserve the existing budget '
                 'before launching workers.')
        argv = ['spore_parallel_sqlite.py', 'run',
                '--workers', settings['workers'], '--max-inflight', settings['max_inflight'],
                '--passes', settings['passes'], '--minutes', settings['minutes'],
                '--budget', settings['budget'], '--quota-wait', settings.get('quota_wait', 1),
                '--provider-retries', settings.get('provider_retries', 2),
                '--reasoning', settings.get('reasoning','high')]
        if args.fixed_inflight:
            argv.append('--fixed-inflight')
        if args.max_functions_per_worker:
            argv += ['--max-functions-per-worker', args.max_functions_per_worker]
        return execute(repo, *argv, env=env, check=False)
    if mode in ('plan', 'status'):
        argv = ['spore_parallel_sqlite.py', mode]
        if mode == 'plan':
            argv += ['--workers', args.workers or saved.get('workers', 5)]
        return execute(repo, *argv, env=with_scope(os.environ.copy(), scope_settings(args, saved)), check=False)
    if mode == 'recover':
        argv = ['spore_recover_provider_attempts.py', '--show', args.show]
        if args.apply:
            argv.append('--apply')
        return execute(repo, *argv, check=False)
    if mode == 'progress':
        return execute(repo, 'spore_matched_bytes.py', check=False)
    if mode == 'doctor':
        print('Upstream checkout:', repo)
        print('OpenCode:', shutil.which('opencode') or 'MISSING')
        print('Python:', python_for(repo))
        print('SQLite:', 'present' if (repo/'work/scratch/codex-pass1/spore.sqlite3').is_file() else 'missing')
        print('Module scope:', ', '.join(scope_settings(args, saved)['modules']))
        print('Checker lock:', 'installed' if 'SPGONZAGO_SQLITE_CHECK_LOCK_V1' in (repo/'tools/contrib/check.py').read_text() else 'missing')
        print('Credential:', 'configured' if os.environ.get('OPENROUTER_API_KEY') or KEY_FILE.is_file() else 'missing')
        if args.live:
            env = os.environ.copy()
            env['OPENROUTER_API_KEY'] = get_key()
            return execute(repo, 'work/scratch/spore_first_pass_sqlite.py', 'doctor', '--live', env=env, check=False)
        return 0


def command_diagnostics(args):
    repo = repo_path(args)
    work = repo/'work/scratch/codex-pass1'
    default = Path.home()/'Downloads'/f'spore-agentic-diagnostics-{STAMP()}.zip'
    destination = Path(args.output).expanduser().resolve() if args.output else default
    destination.parent.mkdir(parents=True, exist_ok=True)
    sources = ['work/scratch/codex-pass1/queue.csv', 'work/scratch/codex-pass1/results.csv',
               'work/scratch/codex-pass1/openrouter-budget.json',
               'spore_parallel_sqlite.py', 'spore_recover_provider_attempts.py',
               'work/scratch/spore_first_pass_sqlite.py', 'work/scratch/spore_sqlite_store.py',
               'work/scratch/spore_provider_log.py', 'tools/contrib/check.py']
    # Don't include raw JSONL logs, key files, config files, or source trees.
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as z:
        database = work/'spore.sqlite3'
        if database.is_file():
            with tempfile.TemporaryDirectory(prefix='spore-db-snapshot-') as d:
                snapshot = Path(d)/'spore.sqlite3'
                with sqlite3.connect(database) as src, sqlite3.connect(snapshot) as dst:
                    src.backup(dst)
                z.write(snapshot, 'state/spore.sqlite3')
        for relative in sources:
            p = repo/relative
            if p.is_file():
                z.write(p, 'checkout/'+relative)
        z.writestr('README.txt',
                   'Spore Agentic Decompiler diagnostic bundle. Contains SQLite attempt history, '
                   'queue, budget metadata and scripts. Does not contain the API key. '
                   'Review before uploading. OpenCode JSONL logs are not included.\n')
    print('Created:', destination)
    print('The archive contains attempt history. No API key or raw OpenCode logs are included.')


def main(argv=None):
    ap = argparse.ArgumentParser(description='Standalone control plane for NoahBPeterson/spore-decomp')
    sub = ap.add_subparsers(dest='command', required=True)
    setup = sub.add_parser('setup', help='Configure key, install engine, and initialize or reuse SQLite state')
    setup.add_argument('--repo', help='Existing spore-decomp checkout path')
    setup.add_argument('--change-key', action='store_true', help='Prompt to replace the stored API key')
    for name, type_ in [('workers', int), ('max-inflight', int), ('passes', int), ('minutes', int), ('budget', float)]:
        setup.add_argument('--'+name, type=type_)
    add_scope_arguments(setup)
    rotation = sub.add_parser('rotate-key', help='Authorize an already-configured replacement OpenRouter key without resetting spending')
    rotation.add_argument('--repo')
    rotation.add_argument('--save-key', action='store_true',
                          help='Also save an environment-supplied key to the private 0600 key file for dashboard launches')
    r = sub.add_parser('run', help='Run paid matching agents, preserving prior results')
    r.add_argument('--repo')
    for name, type_ in [('workers', int), ('max-inflight', int), ('passes', int), ('minutes', int), ('budget', float)]:
        r.add_argument('--'+name, type=type_)
    add_scope_arguments(r)
    r.add_argument('--max-functions-per-worker', type=int, default=0)
    r.add_argument('--fixed-inflight', action='store_true', help='Disable adaptive Sail congestion control')
    plan = sub.add_parser('plan', help='Preview eligible functions and worker split')
    plan.add_argument('--repo'); plan.add_argument('--workers', type=int)
    add_scope_arguments(plan)
    for action in ('status', 'progress', 'doctor'):
        p = sub.add_parser(action)
        p.add_argument('--repo')
        if action == 'doctor':
            p.add_argument('--live', action='store_true', help='Validate OpenRouter key over network; no inference')
    scope = sub.add_parser('scope', help='Preview or append functions for selected modules without removing existing history')
    scope.add_argument('--repo')
    scope.add_argument('--apply', action='store_true', help='Back up DB, append new targets and save module selection')
    add_scope_arguments(scope)
    modules = sub.add_parser('modules', help='List confidently classified modules from the upstream checkout')
    modules.add_argument('--repo')
    modules.add_argument('--min-confidence', type=float)
    dashboard = sub.add_parser('dashboard', help='Run a read-only localhost HTTP activity dashboard')
    dashboard.add_argument('--repo')
    dashboard.add_argument('--port', type=int, default=8765)
    dashboard.add_argument('--control', action='store_true', help='Enable localhost start/stop run controls (read-only by default)')
    recover = sub.add_parser('recover', help='Preview or apply conservative VOID_PROVIDER annotations')
    recover.add_argument('--repo'); recover.add_argument('--apply', action='store_true')
    recover.add_argument('--show', type=int, default=20)
    diag = sub.add_parser('diagnostics', help='Create a ZIP of existing SQLite state and scripts for review')
    diag.add_argument('--repo'); diag.add_argument('--output', help='Output path, defaults to ~/Downloads')
    args = ap.parse_args(argv)
    if args.command == 'setup':
        return command_setup(args)
    if args.command == 'rotate-key':
        return command_rotate_key(args)
    if args.command == 'dashboard':
        from spore_dashboard import serve
        return serve(repo_path(args), args.port, control=args.control)
    if args.command == 'diagnostics':
        return command_diagnostics(args)
    return command_run(args, args.command)


if __name__ == '__main__':
    sys.exit(main())
