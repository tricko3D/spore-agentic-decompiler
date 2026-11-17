"""Conservative classifier for provider-only OpenCode JSONL failures."""
from datetime import datetime
import json
from pathlib import Path

LOGS = Path.cwd() / 'work' / 'scratch' / 'codex-pass1' / 'logs'


def provider_pressure(path: Path) -> int:
    """Count actual upstream 429s, including those embedded in HTTP 200 streams.

    A partial response with real agent work stays a real attempt; this signal
    only governs the adaptive concurrency ceiling.
    """
    if not path or not path.is_file():
        return 0
    n = 0
    try:
        for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict) or event.get('type') != 'retry':
                continue
            error = event.get('error') or {}
            if not isinstance(error, dict):
                continue
            raw = (error.get('response') or {}).get('body') or ''
            try:
                response = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                continue
            upstream = response.get('error') if isinstance(response, dict) else None
            if isinstance(upstream, dict) and upstream.get('code') == 429:
                n += 1
    except OSError:
        return 0
    return n


def provider_only(path: Path, outcome_utc: str | None = None) -> tuple[bool, str]:
    """Conservative: only provider retries, at least one ttft guard, no model/tool output."""
    try:
        actual = path.resolve(strict=True)
    except OSError:
        return False, 'missing log'
    if not actual.is_relative_to(LOGS.resolve()):
        return False, 'log outside expected directory'
    events = []
    try:
        for line in actual.read_text(encoding='utf-8', errors='replace').splitlines():
            try:
                e = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(e, dict):
                events.append(e)
    except OSError as e:
        return False, f'log unreadable: {e}'
    if not events:
        return False, 'empty/non-JSON log'
    if outcome_utc is not None:
        try:
            t = min(e['timestamp'] for e in events
                    if isinstance(e.get('timestamp'), (int, float))) / 1000
            end = datetime.fromisoformat(outcome_utc.replace('Z', '+00:00')).timestamp()
            delta = end - t
            if not 0 <= delta <= 1200:
                return False, f'log timestamp differs from result ({delta:.0f}s)'
        except (ValueError, TypeError, StopIteration, KeyError):
            return False, 'no trustworthy timestamp match'
    retries = [e for e in events if e.get('type') == 'retry']
    if not retries:
        return False, 'no retry events'
    # A genuine tool invocation or completed model response makes this a real attempt.
    if any(e.get('type') in ('tool_use', 'text', 'reasoning') for e in events):
        return False, 'agent emitted content or used tools'
    guard = 0
    for e in retries:
        err = e.get('error') or {}
        if not isinstance(err, dict):
            return False, 'unrecognized retry event'
        typ = str(err.get('type') or '')
        body = (err.get('response') or {}).get('body') or ''
        try:
            body = json.loads(body) if isinstance(body, str) else body
        except ValueError:
            body = {}
        inner = (body.get('error') or {}) if isinstance(body, dict) else {}
        metadata = inner.get('metadata') or {}
        if metadata.get('provider_code') == 'ttft_deadline_guard' and inner.get('code') == 429:
            guard += 1
        elif typ not in ('provider.rate-limit', 'provider.internal'):
            return False, f'other error type {typ}'
    if guard == 0:
        return False, 'no explicit ttft_deadline_guard in retries'
    return True, f'{len(retries)} provider retries, {guard} ttft_deadline_guard, no agent content'

