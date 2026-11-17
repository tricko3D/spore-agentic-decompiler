#!/usr/bin/env python3
"""Standalone controller for byte-exact Spore decompilation inside an upstream checkout.

Requires Python 3.10+ on Linux or WSL. No third-party Python dependencies.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import importlib.util
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
            argv = (p/'cmdline').read_bytes().replace(b'\x00', b' ').decode('utf-8', 'replace')
        except (PermissionError, OSError):
            continue
        if ('spore_first_pass_sqlite.py' in argv or 'spore_parallel_sqlite.py run' in argv) and 'run' in argv:
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
        if module != '*' and (not isinstance(module, str) or not re.fullmatch(r'[A-Za-z0-9_.+\\-]+', module)):
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
        'minutes': args.minutes if args.minutes is not None else int(previous.get('minutes', 5)),
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



def provider_evidence(repo, snapshot):
    """Summarize local retry logs without exporting prompts or model responses.

    The installed provider classifier is the authority for VOID_PROVIDER. A
    missing or ambiguous log is always UNVERIFIED, never automatically voided.
    """
    classifier = repo/'work/scratch/spore_provider_log.py'
    if not classifier.is_file():
        return {'schema': 1, 'error': 'Provider classifier not installed; run setup',
                'attempts': [], 'counts': {'proven_provider_only': 0, 'unverified': 0}}
    spec = importlib.util.spec_from_file_location('spore_diag_provider', classifier)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Its default LOGS is relative to cwd; point it to the selected checkout.
    module.LOGS = repo/'work/scratch/codex-pass1/logs'
    with sqlite3.connect(snapshot) as db:
        db.row_factory = sqlite3.Row
        reviews_exist = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attempt_reviews'"
        ).fetchone() is not None
        sql = """SELECT a.id, a.va, a.pass_number, a.status, a.utc, a.run_id,
                        a.log_path
                 FROM attempts a
                 WHERE a.status IN ('FAIL','DEFER')"""
        if reviews_exist:
            sql += " AND NOT EXISTS(SELECT 1 FROM attempt_reviews r WHERE r.attempt_id=a.id)"
        sql += " ORDER BY a.id"
        rows = [dict(r) for r in db.execute(sql)]
    entries = []
    by_run = {}
    confirmed = 0
    for row in rows:
        log_path = row['log_path']
        if log_path:
            path = Path(log_path)
            if not path.is_absolute():
                path = repo/path
            ok, reason = module.provider_only(path, row['utc'])
        else:
            ok, reason = False, 'no saved log path'
        confirmed += int(ok)
        run = row['run_id'] or 'legacy'
        totals = by_run.setdefault(run, {'proven_provider_only': 0, 'unverified': 0})
        totals['proven_provider_only' if ok else 'unverified'] += 1
        entries.append({
            'attempt_id': row['id'], 'va': row['va'],
            'pass_number': row['pass_number'], 'status': row['status'],
            'run_id': run,
            'classification': 'PROVEN_PROVIDER_ONLY' if ok else 'UNVERIFIED',
            'evidence': reason,
        })
    return {
        'schema': 1,
        'generated_utc': datetime.now(timezone.utc).isoformat(),
        'explanation': 'Local JSONL classification only; no prompts, model output or raw logs exported',
        'counts': {'proven_provider_only': confirmed,
                   'unverified': len(entries) - confirmed},
        'by_run': by_run, 'attempts': entries,
    }


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
                evidence = provider_evidence(repo, snapshot)
                z.writestr('state/provider-evidence.json', json.dumps(evidence, indent=2) + '\n')
        for relative in sources:
            p = repo/relative
            if p.is_file():
                z.write(p, 'checkout/'+relative)
        z.writestr('README.txt',
                   'Spore Agentic Decompiler diagnostic bundle. Contains SQLite attempt history, '
                   'queue, budget metadata and scripts. Does not contain the API key. '
                   'Review before uploading. The provider-evidence.json file contains only '
                   'local classifications, no raw OpenCode JSONL logs or model output.\n')
    print('Created:', destination)
    print('The archive contains attempt history and sanitized provider evidence.')
    print('No API key, raw OpenCode logs, prompts or model output are included.')


def main(argv=None):
    ap = argparse.ArgumentParser(description='Standalone control plane for NoahBPeterson/spore-decomp')
    sub = ap.add_subparsers(dest='command', required=True)
    setup = sub.add_parser('setup', help='Configure key, install engine, and initialize or reuse SQLite state')
    setup.add_argument('--repo', help='Existing spore-decomp checkout path')
    setup.add_argument('--change-key', action='store_true', help='Prompt to replace the stored API key')
    for name, type_ in [('workers', int), ('max-inflight', int), ('passes', int), ('minutes', int), ('budget', float)]:
        setup.add_argument('--'+name, type=type_)
    add_scope_arguments(setup)
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
    recover = sub.add_parser('recover', help='Preview or apply conservative VOID_PROVIDER annotations')
    recover.add_argument('--repo'); recover.add_argument('--apply', action='store_true')
    recover.add_argument('--show', type=int, default=20)
    diag = sub.add_parser('diagnostics', help='Create a ZIP of existing SQLite state and scripts for review')
    diag.add_argument('--repo'); diag.add_argument('--output', help='Output path, defaults to ~/Downloads')
    args = ap.parse_args(argv)
    if args.command == 'setup':
        return command_setup(args)
    if args.command == 'diagnostics':
        return command_diagnostics(args)
    return command_run(args, args.command)


if __name__ == '__main__':
    sys.exit(main())
