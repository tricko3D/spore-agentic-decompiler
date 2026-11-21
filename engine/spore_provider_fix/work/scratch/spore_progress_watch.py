"""Stream-driven OpenCode watchdog, without an absolute pass deadline.

Only confirmed output token accounting proves an inference is healthy.
Tool/text events keep active local work alive; repeated provider retries alone
never reset the inactivity watchdog.
"""
import json
import os
import signal
import subprocess
import time


def is_provider_429(event):
    if event.get('type') != 'retry':
        return False
    error = event.get('error') or {}
    if not isinstance(error, dict):
        return False
    response = error.get('response') or {}
    if not isinstance(response, dict):
        return False
    body = response.get('body') or ''
    try:
        body = json.loads(body) if isinstance(body, str) else body
    except (ValueError, TypeError):
        return False
    upstream = body.get('error') if isinstance(body, dict) else None
    return isinstance(upstream, dict) and upstream.get('code') == 429


def output_tokens(event):
    if event.get('type') != 'step_finish':
        return 0
    part = event.get('part') or {}
    tokens = part.get('tokens') if isinstance(part, dict) else None
    count = tokens.get('output') if isinstance(tokens, dict) else None
    return count if isinstance(count, (float, int)) and not isinstance(count, bool) and count > 0 else 0


def stop_process_group(proc):
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def watch(proc, log, idle_seconds, on_output=None, poll_seconds=0.5, on_event=None,
          max_tools=None, deadline_epoch=None):
    """Return (state, exit_code), including tool_limit on observed completed calls.

    The cap counts distinct OpenCode tool_use events, which are emitted when
    tools complete. Stop the entire OpenCode process group immediately at the
    limit, leaving source, checker artifacts and JSONL intact.
    """
    if max_tools is not None and max_tools < 1:
        raise ValueError('max_tools must be positive')
    position = 0
    fragment = b''
    confirmed = False
    tools_seen = set()
    tool_count = 0
    last_progress = time.monotonic()
    while True:
        try:
            with log.open('rb') as f:
                f.seek(position)
                chunk = f.read()
                position = f.tell()
        except FileNotFoundError:
            chunk = b''
        if chunk:
            parts = (fragment + chunk).split(b'\n')
            fragment = parts.pop()
            if len(fragment) > 1000000:
                fragment = fragment[-1000000:]
            for line in parts:
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, dict):
                    continue
                if on_event:
                    # Diagnostic bookkeeping must never stop a healthy agent.
                    try:
                        on_event(event)
                    except Exception:
                        # Evidence capture is diagnostic, never a run-stopping policy.
                        pass
                if is_provider_429(event):
                    stop_process_group(proc)
                    return 'provider_429', proc.returncode
                kind = event.get('type')
                if kind == 'tool_use' and max_tools is not None:
                    part = event.get('part') or {}
                    identity = (part.get('callID') or part.get('id')) if isinstance(part, dict) else None
                    if not identity or identity not in tools_seen:
                        if identity:
                            tools_seen.add(identity)
                        tool_count += 1
                        if tool_count >= max_tools:
                            stop_process_group(proc)
                            return 'tool_limit', proc.returncode
                if kind in ('step_start', 'step_finish', 'text', 'reasoning', 'tool_use'):
                    last_progress = time.monotonic()
                if not confirmed and output_tokens(event):
                    confirmed = True
                    if on_output:
                        on_output()
        code = proc.poll()
        if deadline_epoch is not None and time.time() >= deadline_epoch:
            stop_process_group(proc)
            return 'deadline', proc.returncode
        if code is not None:
            return 'complete', code
        if time.monotonic() - last_progress >= idle_seconds:
            stop_process_group(proc)
            return 'idle', proc.returncode
        time.sleep(poll_seconds)
