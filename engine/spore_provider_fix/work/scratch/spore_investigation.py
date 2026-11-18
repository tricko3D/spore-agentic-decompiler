"""Durable, bounded evidence for one byte-exact decompilation investigation.

Unlike OpenCode conversation history, these artifacts survive context rotation.
They preserve the original retail assembly and first-seen C++ exactly, while
refreshing the current candidate and recording checker experiments. Nothing
here changes source files, verifies a MATCH or makes an inference request.
"""
from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone

VA_RE = re.compile(r'^0x[0-9a-fA-F]{8}$')
CHECK_COMMAND = re.compile(
    r'(?:^|[\s;|&])(?:\.?/?[\w./-]*python(?:3(?:\.\d+)?)?|py)'
    r'\s+(?:-[A-Za-z]+\s+)*tools/contrib/check\.py(?:\s|$)'
)
# Evidence is a reference, not an invitation to paste huge output into the chat.
MAX_VERDICT = 900
MAX_DIFF = 18000
MAX_ASM = 80000
MAX_SOURCE = 500000


def location(root, va):
    if not VA_RE.fullmatch(va):
        raise ValueError('Expected canonical function address, e.g. 0x01213b70')
    folder = Path(root) / 'work/scratch/codex-pass1/investigations' / va
    return folder


def _atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(content, encoding='utf-8')
    tmp.replace(path)


def _original_once(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('x', encoding='utf-8') as out:
            out.write(content)
    except FileExistsError:
        pass


def _source(root, source):
    if not source:
        return None
    root = Path(root).resolve()
    path = Path(source).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


def _restore_location(root, va):
    file = location(root, va) / 'source.json'
    if not file.is_file():
        return None
    try:
        data = json.loads(file.read_text(encoding='utf-8'))
        return _source(root, Path(root) / data['path'])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def prepare(root, va, size, source=None):
    """Capture immutable starting evidence once; refresh current work each pass."""
    folder = location(root, va)
    folder.mkdir(parents=True, exist_ok=True)
    src = _source(root, source) or _restore_location(root, va)
    if src:
        _atomic(folder / 'source.json', json.dumps(
            {'path': str(src.relative_to(Path(root).resolve()))}, indent=2) + '\n')
        content = src.read_text(encoding='utf-8', errors='replace')
        if len(content) <= MAX_SOURCE:
            _original_once(folder / 'initial.cpp', content)
            _atomic(folder / 'latest.cpp', content)
        else:
            _original_once(folder / 'initial.cpp',
                           '(Original source exceeded evidence archive size; use the working source file.)\n')
    else:
        _original_once(folder / 'initial.cpp', '(No C++ marker/source existed when this investigation began.)\n')

    asm = folder / 'retail-assembly.txt'
    if not asm.exists():
        image = Path(root) / 'work/SporeApp.analysis.bin'
        script = Path(root) / 'tools/matching/disasm.py'
        original = '(Retail analysis image not installed; original instructions unavailable locally.)\n'
        if image.is_file() and script.is_file():
            try:
                length = max(1, min(int(size), 16384))
                proc = subprocess.run([sys.executable, str(script), str(image), va, str(length)],
                                      cwd=root, capture_output=True, text=True, timeout=15)
                if proc.returncode == 0 and proc.stdout.strip():
                    original = proc.stdout[:MAX_ASM]
                else:
                    original = '(Retail disassembly failed; obtain from local disasm.py.)\n'
            except (OSError, ValueError, subprocess.TimeoutExpired):
                original = '(Retail disassembly temporarily unavailable.)\n'
        # Never permanently freeze a missing image as an immutable baseline.
        if original.startswith('('):
            _atomic(asm, original)
        else:
            _original_once(asm, original)

    # If an image appeared after the first run, fill the assembly baseline.
    if asm.is_file() and asm.read_text(encoding='utf-8', errors='replace').startswith('('):
        image = Path(root) / 'work/SporeApp.analysis.bin'
        script = Path(root) / 'tools/matching/disasm.py'
        if image.is_file() and script.is_file():
            try:
                length = max(1, min(int(size), 16384))
                proc = subprocess.run([sys.executable, str(script), str(image), va, str(length)],
                                      cwd=root, capture_output=True, text=True, timeout=15)
                if proc.returncode == 0 and proc.stdout.strip():
                    _atomic(asm, proc.stdout[:MAX_ASM])
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass

    _original_once(folder / 'keep.md', (
        '# Confirmed findings worth keeping\n\n'
        'Record concise, concrete findings only: tested hypothesis, exact checker '
        'result/flags, what remains unknown, and the next promising experiment.\n'
        'Do not paste entire shell output or repeat a search transcript.\n'))
    _original_once(folder / 'experiments.md', '# Checker experiments (automatic)\n\n')
    refresh(root, va)
    return folder


def refresh(root, va):
    """Update latest source and an original-to-current unified diff, without editing C++."""
    folder = location(root, va)
    source = _restore_location(root, va)
    initial = folder / 'initial.cpp'
    if source and source.is_file():
        latest = source.read_text(encoding='utf-8', errors='replace')
        if len(latest) <= MAX_SOURCE:
            _atomic(folder / 'latest.cpp', latest)
        if initial.is_file() and len(latest) <= MAX_SOURCE:
            old = initial.read_text(encoding='utf-8', errors='replace')
            diff = ''.join(difflib.unified_diff(
                old.splitlines(keepends=True), latest.splitlines(keepends=True),
                fromfile='initial.cpp', tofile='latest.cpp'))
            _atomic(folder / 'source-diff.patch',
                    diff[:MAX_DIFF] + ('\n[Additional diff omitted; working C++ is authoritative]\n'
                                      if len(diff) > MAX_DIFF else ''))
    rel = str(source.relative_to(Path(root).resolve())) if source else '(Not located yet)'
    _atomic(folder / 'README.md', (
        f'# Byte-exact investigation {va}\n\n'
        'This is the durable investigation reference, NOT the full model transcript.\n'
        f'Working C++ source: {rel} (authoritative; do not overwrite from the archive)\n\n'
        '* retail-assembly.txt: original retail instructions, immutable when available\n'
        '* initial.cpp: original first-seen C++ source, immutable\n'
        '* latest.cpp: snapshot of latest working C++ (working source is authoritative)\n'
        '* source-diff.patch: delta from initial to latest C++\n'
        '* experiments.md: actual checker runs with concise observed outcomes\n'
        '* keep.md: hypotheses/results you choose to preserve, maintained by agent\n\n'
        'Read only these relevant artifacts. Avoid broad searches or printing entire '
        'logs when a small targeted query is sufficient.\n'))


def record_checker_event(root, va, event, seen=None):
    """Capture only completed *executions* of check.py, not reads of check.py's source.

    Return True when a new experiment was appended. This is best-effort
    bookkeeping and must never decide whether a candidate matches.
    """
    if not isinstance(event, dict) or event.get('type') != 'tool_use':
        return False
    part = event.get('part') or {}
    state = part.get('state') or {}
    inp = state.get('input') or {}
    command = str(inp.get('command') or inp.get('cmd') or '')
    output = state.get('output')
    if not CHECK_COMMAND.search(command) or not isinstance(output, str) or not output.strip():
        return False
    if state.get('status') not in (None, 'completed'):
        return False
    identity = hashlib.sha256((command + '\n' + output).encode('utf-8')).hexdigest()
    if seen is not None:
        if identity in seen:
            return False
        seen.add(identity)
    folder = location(root, va)
    folder.mkdir(parents=True, exist_ok=True)
    selected = []
    for line in output.splitlines():
        value = line.strip()
        if re.search(r'\b(?:MATCH|NO MATCH|COMPILE FAILED|size\s*[=:]|diffs?\s*[=:]|flags|offsets)\b',
                     value, re.IGNORECASE):
            selected.append(value[:280])
        if len(selected) >= 7:
            break
    detail = '\n'.join(selected) if selected else output.strip()[:MAX_VERDICT]
    record = f'- checker command: {command[:350]}\n  result: {detail[:MAX_VERDICT].replace(chr(10), chr(10)+"  ")}\n'
    # Capture the C++ candidate near this observed check event. This is a
    # best-effort post-check snapshot: tool delivery can race the next edit,
    # so do NOT claim the snapshot necessarily caused the byte difference.
    source = _restore_location(root, va)
    if source and source.is_file():
        try:
            cpp = source.read_text(encoding='utf-8', errors='replace')
            if len(cpp) <= 80000:
                digest = hashlib.sha256(cpp.encode('utf-8')).hexdigest()[:20]
                candidate = folder / 'candidates' / (digest + '.cpp')
                _original_once(candidate, cpp)
                record += f'  post-check source snapshot: candidates/{digest}.cpp\n'
                previous_file = folder / 'latest.cpp'
                if previous_file.is_file():
                    old = previous_file.read_text(encoding='utf-8', errors='replace')
                    if old != cpp:
                        diff = ''.join(difflib.unified_diff(
                            old.splitlines(keepends=True), cpp.splitlines(keepends=True),
                            fromfile='previous-snapshot', tofile='post-check-snapshot'))
                        patch = folder / 'candidate-diffs' / (digest + '.patch')
                        _original_once(patch, diff[:12000])
                        record += f'  delta from prior snapshot: candidate-diffs/{digest}.patch\n'
        except OSError:
            pass
    with (folder / 'experiments.md').open('a', encoding='utf-8') as f:
        f.write(record + '\n')
    try:
        refresh(root, va)
    except OSError:
        pass
    return True


def final_result(root, va, pass_number, status, checker):
    folder = location(root, va)
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / 'experiments.md').open('a', encoding='utf-8') as out:
        out.write(f'- Pass {pass_number} independently checked: '
                  f'{status} | {str(checker).replace(chr(10), " ")[:MAX_VERDICT]}\n')
    refresh(root, va)


def session_for_pass(root, va, pass_number, stored_session):
    """Reuse only within a pass; next pass begins with durable evidence, not old chat."""
    meta = location(root, va) / 'session.json'
    try:
        row = json.loads(meta.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return None
    if (row.get('pass_number') == pass_number
            and row.get('session_id') == stored_session
            and isinstance(stored_session, str)
            and stored_session.startswith('ses_')):
        return stored_session
    return None


def remember_session(root, va, pass_number, session_id):
    if not isinstance(session_id, str) or not session_id.startswith('ses_'):
        return
    _atomic(location(root, va) / 'session.json',
            json.dumps({'pass_number': pass_number, 'session_id': session_id}) + '\n')
