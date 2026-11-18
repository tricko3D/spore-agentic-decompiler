#!/usr/bin/env python3
"""Resumable Spore triage with OpenRouter DeepSeek Flash, NVIDIA NIM, or Codex."""
import argparse
import csv
import spore_sqlite_store as store
from spore_provider_log import provider_only, provider_pressure
import spore_adaptive as adaptive_ctl
import spore_progress_watch as progress_watch
import fcntl
from contextlib import contextmanager
import glob
import json
from datetime import datetime, timezone
import importlib.util
import hashlib
import urllib.request
import urllib.error
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

ROOT = Path.cwd()
WORK = ROOT / 'work' / 'scratch' / 'codex-pass1'
QUEUE = WORK / 'queue.csv'
RESULTS = WORK / 'results.csv'
LOGS = WORK / 'logs'
RUNTIME = WORK / 'runtime'
PROMPT_FILE = Path(__file__).with_name('spore_first_pass_prompt.txt')
NVIDIA_MODELS = {
    'mistral4': {
        'id': 'mistralai/mistral-large-4-1t-a52b',
        'name': 'Mistral Large 4',
        'slug': 'mistral-large-4-1t-a52b',
        'context': 262144,  # Hosted NVIDIA free endpoint currently advertises 262K
    },
    'nemotron3': {
        'id': 'nvidia/nemotron-3-ultra-550b-a55b',
        'name': 'Nemotron 3 Ultra 550B',
        'slug': 'nemotron-3-ultra-550b-a55b',
        'context': 1000000,
    },
}
NVIDIA_BASE_URL = 'https://integrate.api.nvidia.com/v1'
OPENROUTER_MODEL = 'deepseek/deepseek-v4.1-flash-1017'
OPENROUTER_PROVIDER = 'sail-research'
OPENROUTER_API = 'https://openrouter.ai/api/v1'
BUDGET_STATE = WORK / 'openrouter-budget.json'
MODULE_MAP = ROOT / 'work' / 'migrate' / 'modmap.json'
# Scope is selected by the standalone controller and inherited by child workers.
# The legacy default stays on SPGonzago to protect existing workflows.
TARGET_MODULES = tuple(x.strip() for x in os.environ.get('SPORE_TARGET_MODULES', 'SPGonzago').split(';') if x.strip())
MIN_MODULE_CONFIDENCE = float(os.environ.get('SPORE_MIN_MODULE_CONFIDENCE', '0.70'))


def module_scope():
    return 'all confidently classified modules' if '*' in TARGET_MODULES else ', '.join(TARGET_MODULES)

PARALLEL_RUNNER_VERSION = 'SPGONZAGO_SQLITE_V1'
PASS_LIMIT_STATE = WORK / 'pass-limit.json'  # legacy only
DB_PATH = store.DB


@contextmanager
def locked(name, exclusive=True):
    """Cross-process advisory lock for shared CSV, budget and settings data."""
    WORK.mkdir(parents=True, exist_ok=True)
    path = WORK / name
    with path.open('a+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def guard_other_run():
    """Do not mix a serial SQLite process with the parallel launcher."""
    if os.environ.get('SPORE_SQLITE_RUN_ID'):
        return
    with (WORK / 'sqlite-parallel.lock').open('a+') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit('Parallel SQLite workers are running; do not start a serial run.')
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def run_info():
    return os.environ.get('SPORE_SQLITE_RUN_ID'), int(os.environ.get('SPORE_SQLITE_WORKER_IDX', '0'))


# A parallel run shares one ID across all workers. Serial runs receive a
# unique ID for their invocation, never inheriting a prior run's spending.
_SERIAL_BUDGET_ID = 'serial-' + uuid.uuid4().hex


def budget_session_id():
    return os.environ.get('SPORE_SQLITE_RUN_ID') or _SERIAL_BUDGET_ID


def assigned_worker(va):
    rid,idx=run_info()
    return store.assigned(va,rid,idx)


def save_pass_limit(passes):
    store.set_pass_limit(passes)


def current_pass_limit():
    return store.pass_limit()


class ManifestIndex:
    """Index all repository manifest entries, cached by file size/mtime.

    Refreshes just before every agent invocation, so functions matched by
    other work since queue initialization cannot consume another API call.
    Does not include equivalent.txt or nonmatching.txt: those are not
    byte-exact manifest claims.
    """
    def __init__(self):
        self._files = {}
        self.addresses = set()

    def refresh(self):
        # Canonical paths plus module-specific .manifest.txt files.
        files = set(ROOT.glob('match/**/manifest.txt'))
        files.update(ROOT.glob('match/**/*.manifest'))
        files.update(ROOT.glob('match/**/*.manifest.txt'))
        files.update(ROOT.glob('match/clones.txt'))
        files.update(ROOT.glob('src/game/**/manifest.txt'))
        files.update(ROOT.glob('src/game/**/*.manifest.txt'))
        files = {p for p in files if p.is_file()}

        for old in set(self._files) - files:
            del self._files[old]
        for path in files:
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            cached = self._files.get(path)
            if cached and cached[0] == signature:
                continue
            addresses = set()
            with path.open(encoding='utf-8', errors='replace') as stream:
                for line in stream:
                    words = line.split('#', 1)[0].split()
                    if len(words) < 3:
                        continue
                    # Official manifest format: SOURCE SYMBOL VA [FLAGS...].
                    try:
                        addresses.add(f'0x{int(words[2], 16):08x}')
                    except ValueError:
                        continue
            self._files[path] = (signature, addresses)
        self.addresses = set().union(*(row[1] for row in self._files.values()))
        return self.addresses


class ModuleIndex:
    """Fail-closed classifier for selected upstream modules.

    Prefer the precise generated modmap when available. When the development
    PDB and work/xmatch/retail.json are absent, use only unambiguous source
    anchors already present in src/game/<module>/; conservatively interpolate
    between matching nearby anchors. No broad address-range guesses.
    """
    SOURCE_MARKER = re.compile(r'(?m)^\s*//\s*@\s*0x([0-9a-fA-F]{6,8})\b')

    def __init__(self, minimum=MIN_MODULE_CONFIDENCE):
        self.minimum = float(minimum)
        self._signature = None
        self.mode = None
        self.functions = {}
        self.anchor_vas = []
        self.anchor_rows = []
        self.component_ranges = []
        self._cache = {}

    def _load_source_anchors(self):
        """Build an independent local index, requiring neither PDB nor xmatch."""
        from bisect import bisect_left
        import collections
        source_root = ROOT / 'src' / 'game'
        anchors = collections.defaultdict(set)
        if not source_root.is_dir():
            sys.exit('Cannot classify modules: src/game source tree not found. '
                     'No API requests will be made.')

        # The migration has source files in src/game/<module>/<object>.cpp.
        # Every `// @ 0xVA` is a positive owner-specific anchor.
        src_count = 0
        for path in source_root.rglob('*'):
            if not path.is_file() or path.suffix.lower() not in ('.cpp', '.c', '.cc', '.cxx'):
                continue
            rel = path.relative_to(source_root)
            if len(rel.parts) < 2 or rel.parts[0].lower() in ('include', '_unassigned'):
                continue
            src_count += 1
            module = rel.parts[0]
            try:
                content = path.read_text(encoding='utf-8', errors='replace')
            except OSError as e:
                sys.exit(f'Cannot inspect source anchors ({path}): {e}')
            for match in self.SOURCE_MARKER.finditer(content):
                va = int(match.group(1), 16)
                anchors[va].add((module, str(path)))

        # Existing module bookkeeping may identify additional functions,
        # including source-free partials. These are not necessarily MATCH,
        # which is why their VAs are not added to the manifest exclusion set.
        for path in source_root.rglob('*.txt'):
            if path.name not in ('manifest.txt', 'nonmatching.txt', 'partial.txt'):
                continue
            rel = path.relative_to(source_root)
            if len(rel.parts) < 2 or rel.parts[0].lower() in ('include', '_unassigned'):
                continue
            module = rel.parts[0]
            try:
                lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
            except OSError as e:
                sys.exit(f'Cannot inspect module notes ({path}): {e}')
            for line in lines:
                fields = line.split('#', 1)[0].split()
                index = 2 if path.name == 'manifest.txt' else 0
                if len(fields) <= index:
                    continue
                try:
                    va = int(fields[index], 16)
                except ValueError:
                    continue
                # Notes alone identify the module but do not prove object identity.
                anchors[va].add((module, None))

        if not anchors:
            sys.exit('No usable module anchors in src/game. Cannot safely filter the queue. '
                     'No API requests will be made.')

        self.anchor_vas = sorted(anchors)
        self.anchor_rows = []
        for va in self.anchor_vas:
            memberships = anchors[va]
            modules = {mod for mod, _ in memberships}
            if len(modules) == 1:
                mod = next(iter(modules))
                files = {f for _mod, f in memberships if f is not None}
                # Source marker identifies precise object only if it is unique.
                source = next(iter(files)) if len(files) == 1 else None
                self.anchor_rows.append((mod, source))
            else:
                # Conflicting anchors act as barriers, never silently removed.
                self.anchor_rows.append(('AMBIGUOUS', None))

        # Explicit middleware ranges are hard exclusions, regardless of
        # source-anchor flanks or source migration glitches.
        components = ROOT / 'symbols' / 'components.json'
        if components.is_file():
            try:
                data = json.loads(components.read_text(encoding='utf-8'))
                ranges = []
                for name, spans in data.items():
                    if name.startswith('_') or not isinstance(spans, list):
                        continue
                    for pair in spans:
                        if isinstance(pair, list) and len(pair) == 2:
                            ranges.append((int(pair[0], 16), int(pair[1], 16)))
                self.component_ranges = ranges
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
                sys.exit(f'Invalid symbols/components.json; cannot safely classify: {e}')

        self.mode = 'source-anchors'
        self._cache.clear()
        print(f'No work/migrate/modmap.json: using conservative src/game module anchors '
              f'({len(self.anchor_vas):,} known addresses from {src_count:,} source files).', flush=True)

    def refresh(self):
        if MODULE_MAP.is_file():
            st = MODULE_MAP.stat()
            sig = (st.st_mtime_ns, st.st_size)
            if self.mode != 'modmap' or sig != self._signature:
                try:
                    data = json.loads(MODULE_MAP.read_text(encoding='utf-8'))
                    functions = data['functions']
                    if not isinstance(functions, dict):
                        raise ValueError('functions should be an object')
                except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
                    sys.exit(f'Invalid module map {MODULE_MAP}: {e}')
                self.functions = functions
                self._signature = sig
                self.mode = 'modmap'
            return self
        if self.mode != 'source-anchors':
            self._load_source_anchors()
        return self

    def classification(self, va):
        self.refresh()
        addr = int(va, 16) if isinstance(va, str) else int(va)
        if self.mode == 'modmap':
            row = self.functions.get(f'{addr:08x}') or self.functions.get(f'0x{addr:08x}')
            if not isinstance(row, dict):
                return None, None
            module = str(row.get('module') or '').replace('\\', '/')
            try:
                confidence = float(row.get('confidence', 0))
            except (TypeError, ValueError):
                confidence = 0.0
            return module, confidence

        if addr in self._cache:
            return self._cache[addr]
        if any(lo <= addr < hi for lo, hi in self.component_ranges):
            answer = (None, None)
        else:
            from bisect import bisect_left
            i = bisect_left(self.anchor_vas, addr)
            if i < len(self.anchor_vas) and self.anchor_vas[i] == addr:
                module = self.anchor_rows[i][0]
                answer = (module, 1.0) if module != 'AMBIGUOUS' else (None, None)
            elif not (0 < i < len(self.anchor_vas)):
                answer = (None, None)
            else:
                left_va, right_va = self.anchor_vas[i - 1], self.anchor_vas[i]
                left_mod, left_file = self.anchor_rows[i - 1]
                right_mod, right_file = self.anchor_rows[i]
                gap = right_va - left_va
                # Closest function anchors MUST agree on module ownership.
                if left_mod != right_mod or left_mod == 'AMBIGUOUS':
                    answer = (None, None)
                elif (left_file and right_file and left_file == right_file and
                      gap <= 0x8000 and addr - left_va <= 0x4000 and right_va - addr <= 0x4000):
                    # Same object file on both sides, stronger than rough
                    # library address-range filtering.
                    answer = (left_mod, 0.90)
                elif (i >= 2 and i + 1 < len(self.anchor_vas) and
                      self.anchor_rows[i - 2][0] == left_mod and
                      self.anchor_rows[i + 1][0] == left_mod and
                      gap <= 0x3000 and addr - left_va <= 0x1800 and right_va - addr <= 0x1800):
                    # Two agreeing module anchors on each side and a tight
                    # central gap: useful where module objects interleave.
                    answer = (left_mod, 0.75)
                else:
                    answer = (None, None)
        self._cache[addr] = answer
        return answer

    def eligible(self, va):
        module, confidence = self.classification(va)
        if not module or confidence is None or confidence < self.minimum:
            return False
        top_level = module.split('/', 1)[0].casefold()
        return '*' in TARGET_MODULES or top_level in {t.casefold() for t in TARGET_MODULES}


def require_repo():
    if not (ROOT / 'tools' / 'contrib' / 'task.py').exists():
        sys.exit('Run from the spore-decomp repository root.')


def task_module():
    spec = importlib.util.spec_from_file_location('spore_contrib_task', ROOT / 'tools/contrib/task.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_csv(path, cols, rows):
    raise RuntimeError('SQLite runner never writes CSV. Legacy files are read-only backups.')


def read_csv(path):
    if path == QUEUE:
        return store.read_queue()
    if path == RESULTS:
        return store.read_attempts()
    raise RuntimeError('Unknown CSV compatibility read')


def initialize(max_size):
    raise RuntimeError('Use migrate_spore_sqlite.py to import the existing 930-function queue. Never rerun init.')


def completed():
    return store.latest()


def compact_attempt_note(note, limit=1200):
    """Preserve independent checker evidence even after a lengthy model explanation."""
    content=re.sub(r'\s+', ' ', str(note or '')).strip()
    if len(content)<=limit:
        return content
    head,separator,checker=content.rpartition(' | checker: ')
    if not separator:
        return content[:limit]
    evidence=checker[:220]
    return head[:max(0,limit-len(separator)-len(evidence))].rstrip()+separator+evidence


def record(va, status, effort='', note='', log_path=None):
    note=compact_attempt_note(note)
    rid,idx=run_info()
    store.record(va,status,effort,note,run_id=rid,worker_idx=idx,log_path=log_path)


def status():
    queue = read_csv(QUEUE)
    done = completed()
    counts = {s: sum(r['status'] == s for r in done.values()) for s in ['MATCH', 'FAIL', 'SKIP', 'DEFER']}
    print(f"Queue {len(queue):,} | MATCH {counts['MATCH']:,} | FAIL {counts['FAIL']:,} | "
          f"SKIP {counts['SKIP']:,} | DEFER {counts['DEFER']:,} | Pending {len(queue) - len(done):,}")
    levels = {f'+{i}': sum(r['status'] == 'FAIL' and r['effort'] == f'+{i}' for r in done.values())
              for i in range(1, 6)}
    print('FAIL effort levels:', ', '.join(f'{k}: {v}' for k, v in levels.items()))
    attempt_rows = read_csv(RESULTS)
    print(f'Logged inference outcomes: {sum(r["status"] in ("FAIL", "DEFER", "MATCH", "SKIP") for r in attempt_rows):,} across {len(done):,} functions')
    incidents = store.incident_summary()
    print(f'VOID_PROVIDER (excluded from pass limits): {incidents["invalid_passes"]:,} archived passes '
          f'across {incidents["invalid_functions"]:,} functions | '
          f'New provider incidents: {incidents["provider_incidents"]:,}')
    manifested = ManifestIndex().refresh()
    modules = ModuleIndex()
    allowed = [row for row in queue if modules.eligible(row['va'])]
    eligible_pending = sum(row['va'] not in manifested and row['va'] not in done for row in allowed)
    manifested_in_scope = sum(row['va'] in manifested for row in allowed)
    print(f'{module_scope()} classified queue: {len(allowed):,} | '
          f'Already in manifest: {manifested_in_scope:,} | Eligible pending: {eligible_pending:,}')
    print(f'Other modules or uncertain classifications excluded: {len(queue)-len(allowed):,} '
          f'(confidence threshold {modules.minimum:.2f})')
    # Parallel launches grant fresh passes; historical FAIL/DEFER counts
    # should not hide otherwise eligible targets in the status summary.
    histories = {}
    for r in attempt_rows:
        histories.setdefault(r['va'], []).append(r)
    eligible_retry = sum(
        row['va'] not in manifested and
        (row['va'] not in histories or
         histories[row['va']][-1]['status'] in ('FAIL', 'DEFER'))
        for row in allowed
    )
    print(f'Eligible nonterminal functions for another parallel run: {eligible_retry:,}')
    print('Database:', DB_PATH.relative_to(ROOT), '(SQLite WAL)')
    budget, reserved = store.budget_status()
    if budget:
        print(f"OpenRouter spend since baseline (last checked): ${budget['last_usage']-budget['baseline_usage']:.4f}; "
              f"in-flight reserved: ${reserved:.2f}")
    active=store.claimed()
    if active:
        print(f'Active function claims: {len(active)}')


def match_recorded(t, va):
    # check.py writes a manifest entry only on an exact byte match.
    source, ident = t.locate(int(va, 16))
    return bool(source and t.status(ident, int(va, 16))[0] == 'byte-exact')


def quota_error(log_path):
    with log_path.open('rb') as f:
        f.seek(max(0, f.seek(0, os.SEEK_END) - 200_000))
        text = f.read().decode('utf-8', 'replace').lower()
    return bool(re.search(r'usage.limit|usage_limit|rate.limit|rate_limit|limit.reached|try again at', text))


def infrastructure_error(text):
    """Never mistake a sandbox/Wine setup failure for a decomp failure."""
    return bool(re.search(
        r'(?:wine(?:server)?\b.{0,120}(?:/run/user/|cannot create|permission denied|mkdir))'
        r'|(?:/run/user/\d+/wine)'
        r'|(?:sandbox.{0,80}(?:permission denied|read.only|operation not permitted))',
        text, re.IGNORECASE | re.DOTALL))


def log_tail(log_path):
    with log_path.open('rb') as f:
        f.seek(max(0, f.seek(0, os.SEEK_END) - 200_000))
        return f.read().decode('utf-8', 'replace')


def nvidia_opencode_config(model='mistral4', reasoning='medium'):
    """Ephemeral OpenCode config; never persists API keys to the repository.

    NVIDIA free endpoints use OpenAI-compatible Chat Completions. NVIDIA's
    trial model IDs are different from Mistral's own vendor API model IDs.
    """
    item = NVIDIA_MODELS[model]
    body = ({'reasoning_effort': reasoning} if model == 'mistral4' else
            {'chat_template_kwargs': {
                'enable_thinking': True,
                'force_nonempty_content': True,
            }})
    return {
        '$schema': 'https://opencode.ai/config.json',
        'share': 'disabled',
        'model': f"spore_nvidia/{item['slug']}",
        'providers': {
            'spore_nvidia': {
                'name': 'NVIDIA NIM (Spore first pass)',
                'env': ['NVIDIA_API_KEY'],
                'package': '@opencode/ai/providers/openai-compatible',
                'settings': {'baseURL': NVIDIA_BASE_URL},
                'body': body,
                'models': {item['slug']: {
                    'modelID': item['id'],
                    'name': item['name'],
                    'limit': {'context': item['context'], 'output': 32768},
                    'capabilities': {'tools': True, 'input': ['text'], 'output': ['text']},
                }},
            },
        },
        'permissions': [
            {'action': 'shell', 'resource': 'git commit *', 'effect': 'deny'},
            {'action': 'shell', 'resource': 'git push *', 'effect': 'deny'},
            {'action': 'shell', 'resource': 'git reset --hard *', 'effect': 'deny'},
        ],
    }


def openrouter_config(reasoning='high'):
    """Use native OpenRouter OpenCode provider, forcing Sail Research only.

    The API token is read from OPENROUTER_API_KEY in the subprocess environment,
    never written into configuration or logs by this runner.
    """
    return {
        '$schema': 'https://opencode.ai/config.json',
        'share': 'disabled',
        'model': 'openrouter/' + OPENROUTER_MODEL,
        'providers': {
            'openrouter': {
                'models': {
                    OPENROUTER_MODEL: {
                        'name': 'DeepSeek V4.1 Flash 1017 (Sail only)',
                        'modelID': OPENROUTER_MODEL,
                        'settings': {'reasoningEffort': reasoning},
                        'body': {
                            'provider': {
                                'order': [OPENROUTER_PROVIDER],
                                'allow_fallbacks': False,
                            },
                            'reasoning': {'effort': reasoning},
                        },
                    },
                },
            },
        },
        'permissions': [
            {'action': 'shell', 'resource': 'git commit *', 'effect': 'deny'},
            {'action': 'shell', 'resource': 'git push *', 'effect': 'deny'},
            {'action': 'shell', 'resource': 'git reset --hard *', 'effect': 'deny'},
        ],
    }


def openrouter_get_key():
    """Get actual OpenRouter charged usage, fail closed on missing data.

    Other requests made with this same API key also contribute to usage. This
    is a conservative soft guard, not a per-request hard cap; use the key limit
    in the OpenRouter dashboard to enforce a true upper bound.
    """
    key = os.environ.get('OPENROUTER_API_KEY', '').strip()
    if not key:
        raise RuntimeError('OPENROUTER_API_KEY is missing')
    req = urllib.request.Request(
        OPENROUTER_API + '/key',
        headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            payload = json.load(res)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'OpenRouter /key HTTP {e.code}') from None
    except (OSError, ValueError, TimeoutError) as e:
        raise RuntimeError(f'OpenRouter /key request failed: {type(e).__name__}: {e}') from None
    data = payload.get('data', {})
    if not isinstance(data, dict) or isinstance(data.get('usage'), bool) or not isinstance(data.get('usage'), (float, int)):
        raise RuntimeError('OpenRouter /key did not provide numeric usage; refusing to proceed without a spending guard')
    return data


def budget_progress(budget):
    """Refresh OpenRouter key spending into the shared SQLite budget record."""
    key_hash=hashlib.sha256(os.environ['OPENROUTER_API_KEY'].encode()).hexdigest()[:20]
    current=openrouter_get_key()
    usage=float(current['usage'])
    remaining=current.get('limit_remaining')
    ok,spent,reserved=store.update_budget(
        key_hash,usage,budget,remaining,run_id=budget_session_id())
    print(f'OpenRouter charged in THIS run: ${spent:.4f} / ${budget:.2f} '
          f'session budget (in-flight reserve ${reserved:.2f})',flush=True)
    if isinstance(remaining,(float,int)) and not isinstance(remaining,bool):
        print(f'OpenRouter KEY limit remaining: ${float(remaining):.4f}',flush=True)
        if float(remaining) < 0.10:
            print('OpenRouter KEY has less than the $0.10 required inference reserve. '
                  'The local session budget cannot override this provider limit.',flush=True)
    return ok


def parse_opencode_log(log_path):
    """Extract final user-facing answer and session errors from OpenCode JSONL.

    Only text events count as model answers, tool-use logs and reasoning do not.
    Return the last text event so an intermediate 'FAIL' cannot win over a
    later final message, and check all error events even when exit code is 0.
    """
    last_text = ''
    problems = []
    text_events = 0
    with log_path.open('r', encoding='utf-8', errors='replace') as f:
        for line in f:
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                # Stderr diagnostics can appear mixed into the JSONL stream.
                continue
            if not isinstance(event, dict):
                continue
            if event.get('type') == 'text':
                value = event.get('part', {}).get('text')
                if isinstance(value, str) and value.strip():
                    last_text = value
                    text_events += 1
            elif event.get('type') == 'error':
                error = event.get('error', {})
                data = error.get('data', {}) if isinstance(error, dict) else {}
                if not isinstance(data, dict):
                    data = {}
                problems.append(f"{data.get('statusCode', '')} {data.get('message', '')} {error.get('name', '')}" if isinstance(error, dict) else str(error))
    return last_text, problems, text_events


def classify_failure(log, code, backend, message=''):
    tail = log_tail(log)
    if infrastructure_error(message + '\n' + tail):
        return 'INFRA', 'Wine/sandbox environment error', log
    if re.search(r'(?i)(?:\b429\b|rate.?limit|quota|usage.?limit|resource.?exhausted|too many requests)',
                 message + '\n' + tail):
        return 'QUOTA', 'Provider rate/quota limit', log
    if re.search(r'(?i)(?:\b401\b|\b403\b|unauthorized|invalid.api.key|authentication failed)',
                 message + '\n' + tail):
        return 'ERROR', 'API authentication/permissions error', log
    if re.search(r'(?i)(?:\b402\b|insufficient.credits|insufficient.balance|payment.required)', message + '\n' + tail):
        return 'ERROR', 'OpenRouter insufficient credits or payment required', log
    return 'ERROR', f'{backend} exit {code}: {message[:160]}', log


def run_one(va, minutes, unsafe, backend, model='mistral4', reasoning='medium',
            pass_number=1, prompt_override=None, slot=None):
    LOGS.mkdir(parents=True, exist_ok=True)
    suffix = model if backend == 'nvidia' else ('deepseek-openrouter' if backend == 'openrouter' else 'codex')
    pass_suffix = f'.pass{pass_number}' if pass_number > 1 else ''
    # Unique immutable logs: retrying a recovered pass must not overwrite the
    # original evidence referenced by an archived SQLite attempt.
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    serial = f'{stamp}.{os.getpid()}'
    log = LOGS / f'{va[2:]}.{suffix}{pass_suffix}.{serial}.jsonl'
    last = LOGS / f'{va[2:]}.{suffix}{pass_suffix}.{serial}.last.txt'
    prompt = (prompt_override if prompt_override is not None else
              PROMPT_FILE.read_text(encoding='utf-8')).replace('{VA}', va)
    if os.environ.get('SPORE_SQLITE_RUN_ID'):
        prompt += f"""
PARALLEL WORKER SAFETY: Other agents are working in this repository concurrently.
Work ONLY on function {va} and its existing source file shown by task.py.
A SQLite source-group claim prevents concurrent edits to this file, but
other workers are editing other source files concurrently. Do not change unrelated
functions in that file, shared headers, global tooling, other slices, queues,
budgets, logs, or other workers' files. Use check.py to record true matches;
only the checker may update manifests and nonmatching notes. If a necessary
fix requires changing a shared header, report SKIP instead.
"""
    env = os.environ.copy()
    # OpenCode may use PWD to select its project, independently of subprocess cwd.
    env['PWD'] = str(ROOT)
    runtime = RUNTIME / ('sqlite-worker-' + os.environ.get('SPORE_SQLITE_WORKER_IDX', 'serial'))
    runtime.mkdir(parents=True, exist_ok=True)
    runtime.chmod(0o700)
    env['XDG_RUNTIME_DIR'] = str(runtime)

    if backend == 'codex':
        cmd = ['codex', 'exec', '--json', '-o', str(last)]
        if unsafe:
            cmd += ['--dangerously-bypass-approvals-and-sandbox']
        else:
            cmd += ['--sandbox', 'workspace-write']
        cmd += [prompt]
    else:
        # OpenCode is not a filesystem sandbox. --unsafe is accepted only
        # for backwards CLI compatibility. Do not mistake --auto for sandboxing.
        if backend == 'openrouter':
            env['OPENCODE_CONFIG_CONTENT'] = json.dumps(openrouter_config(reasoning))
            selected_model = 'openrouter/' + OPENROUTER_MODEL
        else:
            env['OPENCODE_CONFIG_CONTENT'] = json.dumps(nvidia_opencode_config(model, reasoning))
            selected_model = f"spore_nvidia/{NVIDIA_MODELS[model]['slug']}"
        # Reuse this function's OpenCode *conversation* across passes and
        # provider-only retries. A new function gets its own session. Keep
        # --standalone for the pinned ephemeral provider config; sessions are
        # stored in OpenCode's durable database and resumed with --session.
        cmd = ['opencode', 'run', '--standalone', '--auto',
               '--agent', 'build', '--model', selected_model, '--format', 'json']
        sid = store.session_for(va)
        if sid:
            cmd += ['--session', sid]
        else:
            # Explicit title avoids the separate small-model Luna call.
            cmd += ['--title', f'Spore {va}']
        cmd += [prompt]
    def save_session_from_log():
        if backend not in ('openrouter', 'nvidia') or not log.exists():
            return
        try:
            for line in log.read_text(encoding='utf-8', errors='replace').splitlines():
                try:
                    ev = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                sid = ev.get('sessionID') if isinstance(ev, dict) else None
                if isinstance(sid, str) and sid.startswith('ses_'):
                    store.save_session(va, sid)
                    return
        except OSError:
            return

    last.unlink(missing_ok=True)
    with log.open('wb') as output:
        proc = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.STDOUT,
                                env=env, start_new_session=True)
        if os.environ.get('SPORE_SQLITE_RUN_ID'):
            store.record_child(va, proc.pid)
        try:
            rid, _ = run_info()
            def tokens_confirmed():
                if backend == 'openrouter' and slot and os.environ.get('SPORE_ADAPTIVE_INFLIGHT') == '1':
                    message = adaptive_ctl.confirm_output(rid, slot)
                    if message:
                        print('    ADAPTIVE: ' + message, flush=True)
            # --minutes is a legacy CLI name. It now controls inactivity only;
            # old 5-minute settings are clamped to at least 15 minutes.
            idle_minutes = max(15, minutes)
            watch_state, code = progress_watch.watch(
                proc, log, idle_minutes * 60, on_output=tokens_confirmed)
            if watch_state != 'complete':
                save_session_from_log()
                if watch_state == 'provider_429':
                    return 'PROVIDER', 'Confirmed upstream HTTP 429 during inference', log
                return 'IDLE', f'No meaningful progress for {idle_minutes} minutes', log
        except KeyboardInterrupt:
            progress_watch.stop_process_group(proc)
            raise

    save_session_from_log()
    if backend == 'openrouter':
        only, reason = provider_only(log)
        if only:
            return 'PROVIDER', reason, log
    if backend == 'codex':
        message = last.read_text(encoding='utf-8', errors='replace') if last.exists() else ''
        if code != 0:
            return classify_failure(log, code, backend, message)
        if not message.strip():
            if quota_error(log):
                return 'QUOTA', '', log
            return 'ERROR', 'Codex exited without a final answer', log
        if infrastructure_error(message + '\n' + log_tail(log)):
            return 'INFRA', 'Wine/sandbox environment error', log
    else:
        message, problems, text_count = parse_opencode_log(log)
        if problems or code != 0:
            return classify_failure(log, code, backend, '\n'.join(problems))
        if not text_count:
            return 'ERROR', 'OpenCode exited without a text answer (possible truncated JSON events)', log
        last.write_text(message, encoding='utf-8')
        if infrastructure_error(message + '\n' + log_tail(log)):
            return 'INFRA', 'Wine/environment error', log
    return 'OK', message, log


def timed_out_result(row):
    # Recognizes legacy 'FAIL +4: Time limit' rows as well as new DEFER rows.
    return (row.get('status') in ('FAIL', 'DEFER') and
            row.get('note', '').startswith('Time limit ('))


def history_by_va():
    """Append-only attempt history; existing first-pass FAIL/DEFER counts as pass 1.

    Keeping the original results.csv format makes old runs compatible with
    the existing status and reset commands. Every completed pass adds a row.
    """
    out = {}
    for row in read_csv(RESULTS):
        out.setdefault(row['va'], []).append(row)
    return out


def previous_log(va, backend, model, previous_pass):
    suffix = model if backend == 'nvidia' else ('deepseek-openrouter' if backend == 'openrouter' else 'codex')
    pass_suffix = f'.pass{previous_pass}' if previous_pass > 1 else ''
    return LOGS / f'{va[2:]}.{suffix}{pass_suffix}.jsonl'


def prior_checker_feedback(log_path, limit=5500):
    """Recover real check.py output from the previous attempt, if available."""
    if not log_path.is_file():
        return '(No prior tool log found.)'
    last = ''
    try:
        with log_path.open(encoding='utf-8', errors='replace') as f:
            for line in f:
                try:
                    event = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                if event.get('type') != 'tool_use':
                    continue
                part = event.get('part') or {}
                state = part.get('state') or {}
                inputs = state.get('input') or {}
                command = str(inputs.get('command') or inputs.get('cmd') or '')
                if 'tools/contrib/check.py' not in command:
                    continue
                result = state.get('output') or ''
                if isinstance(result, str) and result.strip():
                    last = result
    except OSError:
        return '(Could not read previous log.)'
    return last[-limit:] if last else '(No prior checker output in tool log.)'


def continuation_prompt(va, pass_number, prior_row, prior_log, total_passes=3):
    """Subsequent passes repair the saved code, not start a blank reconstruction."""
    assert 2 <= pass_number <= total_passes
    previous = (prior_row.get('note') or '(No previous explanation.)')[:1000]
    feedback = prior_checker_feedback(prior_log)
    return f"""SELECTED MODULES: {module_scope()}. CONTINUATION PASS {pass_number} OF {total_passes} for {va}.

You are continuing a prior attempt at this SAME function. The previous C++
source and all earlier edits are already on disk. Preserve and improve that
candidate. If there was no source or marker, create a real C++ implementation.
Do NOT restart from scratch merely because the preceding pass failed.
Do not work on any other function, module, or repository. Do not change the
queue/results files, budgets, global scripts, or unrelated code. No git commits,
pushes, resets, or inline assembly. No hard-coded output bytes or fake matches.

Previous result: {prior_row.get('status','?')} {prior_row.get('effort','')} {previous}
Previous log: {prior_log.relative_to(ROOT) if prior_log.is_relative_to(ROOT) else prior_log}
Previous verifier output (may use different flags; verify the exact flags):
{feedback}

FIRST: inspect the existing source and the last checker difference. Run
.venv/bin/python tools/contrib/task.py {va} if you need the task card.
Use tools/contrib/check.py {va} to iterate on the *current* source.
Where the diff suggests compiler configuration issues, test justified flags
(e.g. /O2 /MD /Gy /TP /arch:SSE /fp:fast), but do not assume /fp:fast
is correct for every function. Refer to nearby verified function flags or
local compiler documentation only when relevant.

FOCUSED EDIT-AND-CHECK ITERATIONS: up to THREE check cycles in this pass,
subject to the runner's time limit. Make the first edit promptly. Focus on
actual retail instruction differences: register allocation, x87 versus SSE,
stack spills and local lifetimes, call order, virtual slots and control flow.
Do not waste time on broad PDB searches, unrelated functions or speculative
research. If a test differs, use its assembly diff to guide the next edit.

A MATCH requires an actual successful checker run that records the function
in the repository manifest, not a verbal claim. Report exactly
MATCH: checker confirmed byte-exact, or
FAIL +N: concise verified technical blocker (N = 1..5).
Leave the latest honest C++ candidate in its source file on failure.
"""


def checker_flag_candidates(log_path):
    """Flags the agent actually tried, never a model's unverified verdict."""
    if not log_path or not log_path.is_file():
        return []
    import shlex
    candidates = []
    try:
        for line in log_path.read_text(encoding='utf-8', errors='replace').splitlines():
            try:
                event = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            if event.get('type') != 'tool_use':
                continue
            state = (event.get('part') or {}).get('state') or {}
            inp = state.get('input') or {}
            cmd = str(inp.get('command') or inp.get('cmd') or '')
            if 'tools/contrib/check.py' not in cmd or '--flags' not in cmd:
                continue
            try:
                pieces = shlex.split(cmd)
            except ValueError:
                continue
            for i, token in enumerate(pieces[:-1]):
                if token == '--flags' and pieces[i + 1].startswith('/'):
                    flags = pieces[i + 1]
                    if flags not in candidates:
                        candidates.append(flags)
    except OSError:
        return []
    return candidates[-4:]


def checker_metric_summary(lines):
    """Extract only numbers reported by the independently invoked checker."""
    output='\n'.join(lines)
    sizes=re.findall(r'\bsize\s*[=:]\s*(\d+)\s*/\s*(\d+)\b',output,re.I)
    diffs=re.findall(r'\b(?:diffs?|differences)\s*[=:]\s*(\d+)\b',output,re.I)
    if not sizes or not diffs:
        return ''
    compiled,retail=sizes[-1]
    return f'size={compiled}/{retail} diffs={diffs[-1]}'


def parse_agent_verdict(answer):
    """Parse explicit FAIL +N and SKIP verdicts, including Markdown reports.

    This only parses model explanations. It never accepts a claimed MATCH;
    independent byte verification is still required in process().
    """
    if not isinstance(answer, str):
        return None
    pattern = re.compile(
        r'^[ \t]*(?:#{1,6}[ \t]+|[-*][ \t]+)?'
        r'(?P<fmt>\*\*|__|\x60)?'
        r'(?P<verdict>FAIL[ \t]*\+[1-5]|SKIP)'
        r'(?P<inner_colon>:)?'
        r'(?(fmt)(?P=fmt))'
        r'(?=$|[ \t]|:|—|–|-)'
        r'[ \t]*(?:[:—–-][ \t]*)?'
        r'(?P<reason>.*)$', re.IGNORECASE
    )
    lines = answer.splitlines()
    in_fence = False
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if re.match(r'^(?:\x60{3}|~~~)', stripped):
            in_fence = not in_fence
            continue
        if in_fence or stripped.startswith('>'):
            continue
        match = pattern.match(line)
        if not match:
            continue

        verdict = match.group('verdict').upper().replace(' ', '')
        status = 'FAIL' if verdict.startswith('FAIL') else 'SKIP'
        effort = '+' + verdict.split('+', 1)[1] if status == 'FAIL' else ''
        reason = (match.group('reason') or '').strip()
        if not reason:
            # A heading may contain just the verdict; its technical blocker
            # is then the next substantive line.
            for subsequent in lines[index + 1:]:
                subsequent = subsequent.strip()
                if not subsequent:
                    continue
                if re.match(r'^(?:\x60{3}|~~~|>)', subsequent):
                    break
                reason = subsequent
                break
        reason = re.sub(r'\*\*(.*?)\*\*', r'\1', reason).strip()
        return status, effort, (reason[:1200] or 'No reason supplied')
    return None


def checked_result(va, log_path=None, minutes=2):
    """Independent byte check; never accept a model's MATCH statement alone.

    First try the task/manifest flags, then exact flags seen in genuine prior
    shell tool calls. Record the manifest only after our own MATCH succeeds.
    """
    flag_sets = [None, *checker_flag_candidates(log_path)]
    last_failure = 'No candidate verified'
    for flags in flag_sets:
        cmd = [sys.executable, str(ROOT / 'tools/contrib/check.py'), va, '--dry-run', '--no-quick']
        if flags:
            cmd += ['--flags', flags]
        env = {**os.environ, 'PWD': str(ROOT)}
        try:
            p = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True,
                               timeout=minutes * 60, env=env)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f'Independent checker failed: {type(e).__name__}: {e}'
        lines = (p.stdout + '\n' + p.stderr).strip().splitlines()
        headline = next((x.strip() for x in lines if x.strip()), 'No checker output')
        if p.returncode != 0 or not headline.startswith('MATCH '):
            metrics=checker_metric_summary(lines)
            last_failure = (((metrics + ' | ') if metrics else '') + headline)[:220]
            continue
        if va not in ManifestIndex().refresh():
            # The dry-run independently verified the bytes. Let the checker
            # create the official manifest row for the exact successful flags.
            real_cmd = [x for x in cmd if x != '--dry-run']
            try:
                real = subprocess.run(real_cmd, cwd=ROOT, text=True,
                                      capture_output=True, timeout=minutes * 60, env=env)
            except (OSError, subprocess.TimeoutExpired) as e:
                return False, f'Manifest recording failed: {type(e).__name__}: {e}'
            if real.returncode != 0 or not real.stdout.startswith('MATCH '):
                return False, 'Verification MATCH, but manifest recording failed'
        return va in ManifestIndex().refresh(), headline[:220]
    return False, last_failure


def budget_check_or_wait(budget, adaptive=False):
    """Wait out transient billing API outages, but never bypass the spend guard."""
    delay = 10
    while True:
        try:
            return budget_progress(budget)
        except RuntimeError as exc:
            reason = str(exc)
            transient = ('OpenRouter /key request failed:' in reason or
                         any(f'OpenRouter /key HTTP {code}' in reason for code in (429, 500, 502, 503, 504)))
            if not adaptive or not transient:
                raise
            print(f'Spending guard unavailable ({reason}); retrying in {delay}s. No paid request started.', flush=True)
            time.sleep(delay)
            delay = min(300, delay * 2)


def process(minutes, unsafe, backend, quota_wait, max_functions,
            model='mistral4', reasoning='high', retry_timeouts=False, budget=5.0,
            only_va=None, passes=3, max_inflight=8, provider_retries=2):
    require_repo()
    executable = 'codex' if backend == 'codex' else 'opencode'
    if not shutil.which(executable):
        sys.exit(f'{executable} executable not found in PATH.')
    if backend == 'nvidia' and not os.environ.get('NVIDIA_API_KEY', '').strip():
        sys.exit('NVIDIA_API_KEY not set. Export your build.nvidia.com API key in this shell.')
    if backend == 'openrouter' and not os.environ.get('OPENROUTER_API_KEY', '').strip():
        sys.exit('OPENROUTER_API_KEY not set. Export your OpenRouter API key in this shell.')
    if not PROMPT_FILE.exists():
        sys.exit(f'Prompt file missing: {PROMPT_FILE}')
    if not 1 <= passes <= 24:
        sys.exit('--passes must be between 1 and 24')
    adaptive = backend == 'openrouter' and os.environ.get('SPORE_ADAPTIVE_INFLIGHT') == '1'
    store.ensure_imported()
    guard_other_run()
    # For parallel runs, each VA has its own cumulative ceiling in SQLite.
    # The dashboard's passes value is additional work, never a history reset.
    if not os.environ.get('SPORE_SQLITE_RUN_ID'):
        save_pass_limit(passes)
    queue = read_csv(QUEUE)
    history = history_by_va()
    modules = ModuleIndex()
    modules.refresh()  # fail closed BEFORE any API spending
    allowed = {row['va'] for row in queue if modules.eligible(row['va'])}
    t = task_module()
    manifests = ManifestIndex()
    already_matched = manifests.refresh()
    queued_manifested = sum(row['va'] in already_matched for row in queue if row['va'] in allowed)
    print(f'Scope: {module_scope()}, via {modules.mode}, confidence >= {modules.minimum:.2f}. '
          f'{len(allowed)} / {len(queue)} queued functions in scope.', flush=True)
    print(f'Manifest guard: {queued_manifested} scoped queued function(s) already manifested; '
          f'will not call the agent for them.', flush=True)
    if os.environ.get('SPORE_SQLITE_RUN_ID'):
        print(f'Parallel SQLite worker {os.environ.get("SPORE_SQLITE_WORKER_IDX")}', flush=True)
    print(f'Pass policy: at most {passes} total paid attempts per function, '
          f'including existing first-pass history. No wall-clock deadline; inactivity watchdog after {max(15, minutes)} min.', flush=True)
    if only_va and only_va not in {r['va'] for r in queue}:
        sys.exit(f'{only_va} is not in the queue')
    if only_va and only_va not in allowed:
        module, confidence = modules.classification(only_va)
        sys.exit(f'{only_va} excluded: mapped module={module!r}, confidence={confidence}. '
                 f'Only {module_scope()} at confidence >= {modules.minimum:.2f} is allowed.')
    if backend == 'nvidia':
        print(f"Backend: NVIDIA {NVIDIA_MODELS[model]['name']} via OpenCode ({reasoning} reasoning). "
              'OpenCode shell tools run WITHOUT a sandbox.', flush=True)
    elif backend == 'openrouter':
        print(f'Backend: OpenRouter {OPENROUTER_MODEL} ({OPENROUTER_PROVIDER} only, '
              f'{reasoning} reasoning). OpenCode shell tools run WITHOUT a sandbox.', flush=True)
        try:
            if not budget_check_or_wait(budget, adaptive=adaptive):
                print('Run budget or OpenRouter key limit reached. Stopped without starting a function.')
                return
        except (OSError, ValueError, RuntimeError) as e:
            print(f'Spending guard check failed, stopping: {e}', file=sys.stderr)
            raise SystemExit(3)

    dynamic = bool(os.environ.get('SPORE_SQLITE_RUN_ID'))
    rid, worker_idx = run_info()
    positions = {row['va']: (idx, row) for idx,row in enumerate(queue, start=1)}

    def work_items():
        if not dynamic:
            yield from enumerate(queue, start=1)
            return
        yielded = 0
        while not max_functions or yielded < max_functions:
            va, busy = store.claim_next(rid, worker_idx, passes)
            if va is not None:
                yielded += 1
                yield positions[va]
            elif busy:
                time.sleep(3)
            else:
                return

    processed_functions = 0
    for idx, row in work_items():
        va = row['va']
        if only_va and va != only_va:
            continue
        if not dynamic and not assigned_worker(va):
            continue
        if va not in allowed or not modules.eligible(va):
            if dynamic:
                store.ignore_run_target(rid,va,'out of scope')
                store.release_claim(va,rid,worker_idx)
            continue
        if va in manifests.refresh():
            if dynamic:
                store.ignore_run_target(rid,va,'already in upstream manifest')
                store.release_claim(va,rid,worker_idx)
            if only_va:
                print(f'{va} is already manifested; skipped.', flush=True)
            continue
        previous = store.history_for(va) if dynamic else history.get(va, [])
        if dynamic: history[va] = previous
        if previous and previous[-1]['status'] in ('MATCH', 'SKIP'):
            if dynamic:
                store.ignore_run_target(rid,va,'already terminal')
                store.release_claim(va,rid,worker_idx)
            continue
        # Original single-pass results count toward the limit. A timeout
        # consumes one attempt; quota/infra failures were never recorded.
        attempted = sum(r['status'] in ('FAIL', 'DEFER') for r in previous)
        pass_ceiling = store.run_attempt_limit(rid, va) if dynamic else passes
        if attempted >= pass_ceiling:
            if dynamic:
                store.ignore_run_target(rid,va,'passes exhausted')
                store.release_claim(va,rid,worker_idx)
            if only_va:
                print(f'{va} already used {attempted}/{pass_ceiling} attempts.', flush=True)
            continue
        if max_functions and processed_functions >= max_functions:
            print(f'Stopped after {processed_functions} function(s); queue/results preserved.')
            status()
            return
        processed_functions += 1
        print(f'[{idx}/{len(queue)}] {va} ({row["size"]} bytes), '
              f'previous attempts {attempted}/{pass_ceiling}', flush=True)

        rid,worker_idx=run_info()
        if not dynamic and not store.claim(va,rid,worker_idx,passes):
            print(f'  {va} is already claimed or completed; skipped.',flush=True)
            continue
        try:
            for pass_number in range(attempted + 1, pass_ceiling + 1):
                if va in manifests.refresh() or not modules.eligible(va):
                    print(f'  {va} became manifested or out-of-scope; skipped.', flush=True)
                    break
                if backend == 'openrouter':
                    try:
                        if not budget_check_or_wait(budget, adaptive=adaptive):
                            print('Run budget or OpenRouter key limit reached; queue/results preserved.', flush=True)
                            status()
                            return
                    except (OSError, ValueError, RuntimeError) as e:
                        print(f'Spending guard check failed, stopping: {e}', file=sys.stderr)
                        return

                prior = history.get(va, [])
                prior_row = prior[-1] if prior else None
                old_log = prior_row.get('log_path') if prior_row else None
                prior_log = (Path(old_log) if old_log else
                             previous_log(va, backend, model, pass_number - 1))
                prompt = (continuation_prompt(va, pass_number, prior_row, prior_log, pass_ceiling)
                          if pass_number > 1 else None)
                print(f'  Pass {pass_number}/{pass_ceiling}...', flush=True)
                provider_failures = 0
                while True:
                    reservation = None
                    slot = None
                    try:
                        if backend == 'openrouter' and max_inflight:
                            # Waiting for the slot never consumes the model's 5-minute budget.
                            while slot is None:
                                slot = store.reserve_inference_slot(va, rid, max_inflight)
                                if slot is None:
                                    time.sleep(2)
                        if backend == 'openrouter':
                            reservation = store.reserve_budget(budget_session_id(), va, 0.10, budget)
                            if reservation is None:
                                print('Run budget reserved for other workers or exhausted; stopping.', flush=True)
                                return
                        kind, answer, log = run_one(va, minutes, unsafe, backend,
                                                    model, reasoning, pass_number, prompt, slot=slot)
                    except KeyboardInterrupt:
                        print('\nStopped. Completed passes remain recorded; unfinished pass can be retried.')
                        return
                    finally:
                        store.release_reservation(reservation)
                        store.release_inference_slot(slot)
                    if backend == 'openrouter':
                        if adaptive:
                            pressure = provider_pressure(log)
                            signal = adaptive_ctl.observe(rid, overloaded=kind in ('QUOTA','PROVIDER') or pressure > 0)
                            if signal:
                                print('    ADAPTIVE: ' + signal, flush=True)
                        try:
                            under_budget = budget_check_or_wait(budget, adaptive=adaptive)
                        except (OSError, ValueError, RuntimeError) as e:
                            print(f'Spending guard failed after attempt: {e}; recording this pass before stopping.',
                                  file=sys.stderr)
                            under_budget = None
                    else:
                        under_budget = True
                    if kind in ('QUOTA', 'PROVIDER'):
                        store.record_incident(va, answer or kind, pass_number, log,
                                              run_id=rid, worker_idx=worker_idx)
                        provider_failures += 1
                        print(f'    PROVIDER_ERROR, no attempt counted: {answer}; log: {log}', flush=True)
                        if not under_budget or (not adaptive and (quota_wait == 0 or provider_failures > provider_retries)):
                            print(f'Provider unavailable. {va} still eligible for pass {pass_number}; '
                                  f'stopping worker after {provider_failures} provider failure(s).', flush=True)
                            return
                        if adaptive and dynamic:
                            # Release the failing probe's source claim; other
                            # workers may reclaim this function after backoff.
                            # No matching pass is consumed, and healthy agents
                            # continue with their own checked-out functions.
                            window=adaptive_ctl.snapshot()
                            delay=max(45,int(window['cooldown_until']-time.time()) if window else 45)
                            store.defer_run_target(rid,va,delay)
                            print(f'Deferred {va} for {delay}s; returning work to shared queue.', flush=True)
                            break
                        print('Adaptive Sail cooldown; retrying this pass without consuming it.' if adaptive
                              else f'Waiting {quota_wait} minute(s) before retry in same session...', flush=True)
                        try:
                            if not adaptive:
                                time.sleep(quota_wait * 60)
                        except KeyboardInterrupt:
                            print('\nStopped; valid attempts preserved.')
                            return
                        continue
                    if kind == 'IDLE':
                        print(f'Idle watchdog stopped {va}: {answer}; no pass consumed.', flush=True)
                        if dynamic:
                            store.ignore_run_target(rid, va, 'inactivity watchdog; retry in next run')
                        break
                    if kind == 'INFRA':
                        print(f'Wine/environment failure, stopping without ticking {va}. Log: {log}', file=sys.stderr)
                        return
                    if kind == 'ERROR':
                        print(f'{backend} error, stopping without ticking {va}: {answer}. Log: {log}', file=sys.stderr)
                        return
                    break

                if kind in ('IDLE','QUOTA','PROVIDER') and adaptive and dynamic:
                    # The failed probe or stalled stream never spends a pass;
                    # a delayed provider VA is eligible for another worker.
                    break
                if kind == 'IDLE':
                    break

                # The model can misunderstand its tool output; independently
                # recompile and check the candidate before accepting MATCH.
                is_match, check_note = checked_result(va, log)
                if is_match:
                    outcome = ('MATCH', '', f'Pass {pass_number}: independently byte-exact ({check_note})')
                elif kind == 'TIMEOUT':
                    outcome = ('DEFER', '', f'Pass {pass_number}: time limit ({minutes} min) | checker: {check_note}')
                else:
                    verdict = parse_agent_verdict(answer)
                    if verdict:
                        result_status, effort, explanation = verdict
                        outcome = (result_status, effort,
                                   f'Pass {pass_number}: {explanation} | checker: {check_note}')
                    else:
                        # Some models claim MATCH although no verified manifest
                        # was created; count the inference, not a false success.
                        outcome = ('FAIL', '+3',
                                   f'Pass {pass_number}: unverified model response: {answer[:95]} | checker: {check_note}')
                record(va, *outcome, log_path=log)
                history.setdefault(va, []).append({'va': va, 'status': outcome[0],
                                                      'effort': outcome[1], 'note': outcome[2]})
                print(f'    {outcome[0]} {outcome[1]} {outcome[2]}', flush=True)
                if under_budget is not True:
                    print('Run budget or OpenRouter key guard reached or unavailable after recording pass; stopped.', flush=True)
                    status()
                    return
                if outcome[0] in ('MATCH', 'SKIP'):
                    break
            else:
                print(f'  {va}: exhausted {pass_ceiling} cumulative attempts for this run.', flush=True)
        finally:
            store.release_claim(va,rid,worker_idx)
    print('\nFinished eligible module scope at the configured pass limit.')
    status()

def doctor(backend, model='mistral4', reasoning='high', live=False, budget=5.0):
    print('Repository:', ROOT)
    print('Queue:', 'present' if QUEUE.exists() else 'missing (run init)')
    print('SQLite database:', DB_PATH if DB_PATH.exists() else 'missing (run migration)')
    print('Prompt:', 'present' if PROMPT_FILE.exists() else 'MISSING')
    exe = 'codex' if backend == 'codex' else 'opencode'
    print('Agent executable:', shutil.which(exe) or f'MISSING ({exe})')
    if backend == 'nvidia':
        print('API key:', 'set' if os.environ.get('NVIDIA_API_KEY', '').strip() else 'MISSING')
        print('API URL:', NVIDIA_BASE_URL)
        print('Model:', NVIDIA_MODELS[model]['id'])
        print('Reasoning:', reasoning if model == 'mistral4' else 'provider default')
        print('Transport: OpenCode Chat Completions; shell commands are NOT sandboxed')
        print('Note: this validates configuration only, not NVIDIA API access or model behavior.')
    elif backend == 'openrouter':
        print('API key:', 'set' if os.environ.get('OPENROUTER_API_KEY', '').strip() else 'MISSING')
        print('Model:', OPENROUTER_MODEL)
        print('Routing: Sail Research only, fallbacks disabled')
        print('Reasoning requested:', reasoning, '(provider may not honor specific effort)')
        print('Per-run budget: $%.2f; OpenRouter key limit remains a separate hard cap' % budget)
        print('OpenCode shell tools are NOT sandboxed')
        if live:
            if not os.environ.get('OPENROUTER_API_KEY', '').strip():
                sys.exit('OPENROUTER_API_KEY not set')
            info = openrouter_get_key()
            print('Live OpenRouter key check: success, total key usage $%.4f' % float(info['usage']))
            if info.get('limit_remaining') is not None:
                print('API key limit remaining:', info['limit_remaining'])
            print('No model inference was purchased by this check.')


def main():
    require_repo()
    p = argparse.ArgumentParser(description='Resumable Spore byte-match triage (OpenRouter DeepSeek Flash, NVIDIA, Codex)')
    sub = p.add_subparsers(dest='cmd', required=True)
    init = sub.add_parser('init', help='Create queue from functions without source')
    init.add_argument('--max-size', type=int, default=0, help='Maximum retail function bytes; 0 = all (default)')
    run = sub.add_parser('run', help='Run unmanifested functions in selected modules with confident mapping')
    run.add_argument('--minutes', type=int, default=20, help='Time budget per function (default 5)')
    run.add_argument('--unsafe', action='store_true', help='Disable Codex sandbox; for OpenRouter/NVIDIA, accepted for backwards compatibility only')
    run.add_argument('--backend', choices=['openrouter', 'nvidia', 'codex'], default='openrouter', help='Agent backend (default OpenRouter DeepSeek/OpenCode)')
    run.add_argument('--quota-wait', type=int, default=5, help='Wait this many minutes on 429/quota; 0 = stop with queue unchanged')
    run.add_argument('--max-inflight', type=int, default=8, help='Maximum active OpenRouter inferences across all SQLite workers; 0 disables cap')
    run.add_argument('--provider-retries', type=int, default=2, help='Additional retries for provider-only failures; never counts as an attempt')
    run.add_argument('--max-functions', type=int, default=0, help='Stop after N completed attempts; 0 = entire pending queue')
    run.add_argument('--model', choices=NVIDIA_MODELS, default='mistral4', help='NVIDIA model only; ignored by OpenRouter (which always uses DeepSeek V4.1 Flash 1017)')
    run.add_argument('--reasoning', choices=['low', 'medium', 'high'], default='high', help='Requested reasoning effort (default high; API provider may not honor it)')
    run.add_argument('--passes', type=int, default=3, help='Additional valid attempts per function for a parallel run (1-24; default 3); serial mode keeps cumulative limit')
    run.add_argument('--retry-timeouts', action='store_true', help='Retry previously timed-out functions, including legacy FAIL +4 timeouts; other results remain intact')
    run.add_argument('--budget', type=float, default=5.0, help='Per-run OpenRouter soft spending limit in USD (default $5; the OpenRouter key limit is independent)')
    run.add_argument('--only-va', help='Run only this queued function, useful for testing (e.g. 0x00c69a70)')
    run.add_argument('--run-id', help=argparse.SUPPRESS)
    run.add_argument('--worker-idx', type=int, help=argparse.SUPPRESS)
    check = sub.add_parser('doctor', help='Check local prerequisites without calling an API')
    check.add_argument('--backend', choices=['openrouter', 'nvidia', 'codex'], default='openrouter')
    check.add_argument('--model', choices=NVIDIA_MODELS, default='mistral4')
    check.add_argument('--reasoning', choices=['low', 'medium', 'high'], default='high')
    check.add_argument('--live', action='store_true', help='Check OpenRouter API key and usage (no inference purchased)')
    check.add_argument('--budget', type=float, default=5.0)
    sub.add_parser('status', help='Print progress for the selected module scope')
    reset = sub.add_parser('reset', help='Untick a specific VA (e.g. an infrastructure failure)')
    reset.add_argument('va')
    a = p.parse_args()
    if a.cmd == 'init':
        initialize(a.max_size)
    elif a.cmd == 'run':
        if a.run_id is not None:
            if a.worker_idx is None or a.worker_idx < 0:
                p.error('--run-id requires --worker-idx')
            os.environ['SPORE_SQLITE_RUN_ID']=a.run_id
            os.environ['SPORE_SQLITE_WORKER_IDX']=str(a.worker_idx)
        if a.minutes < 1:
            p.error('--minutes must be >= 1')
        if a.quota_wait < 0:
            p.error('--quota-wait must be >= 0')
        if a.max_functions < 0:
            p.error('--max-functions must be >= 0')
        if a.budget <= 0:
            p.error('--budget must be > 0')
        if not 1 <= a.passes <= 24:
            p.error('--passes must be between 1 and 24')
        only_va = f'0x{int(a.only_va, 16):08x}' if a.only_va else None
        if a.max_inflight < 0 or a.provider_retries < 0:
            p.error('--max-inflight and --provider-retries must be nonnegative')
        process(a.minutes, a.unsafe, a.backend, a.quota_wait, a.max_functions,
                a.model, a.reasoning, a.retry_timeouts, a.budget, only_va,
                a.passes, a.max_inflight, a.provider_retries)
    elif a.cmd == 'doctor':
        doctor(a.backend, a.model, a.reasoning, a.live, a.budget)
    elif a.cmd == 'reset':
        guard_other_run()
        store.ensure_imported()
        va = f'0x{int(a.va,16):08x}'
        if va not in {r['va'] for r in store.read_queue()}:
            sys.exit(f'{va} is not in this pass queue.')
        n=store.reset(va)
        print(f'{va}: removed {n} historical result(s) from SQLite; source/log files untouched.')
    else:
        store.ensure_imported()
        status()


if __name__ == '__main__':
    main()
