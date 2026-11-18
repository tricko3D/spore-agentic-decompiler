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


def watch(proc, log, idle_seconds, on_output=None, poll_seconds=0.5, on_event=None):
    """Return (result, exit_code). Result is complete, provider_429 or idle.

    Parsing is incremental and leaves JSONL source and session state intact.
    Only the first positive token sample triggers the on_output callback.
    """
    position = 0
    fragment = b''
    confirmed = False
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
                if kind in ('step_start', 'step_finish', 'text', 'reasoning', 'tool_use'):
                    last_progress = time.monotonic()
                if not confirmed and output_tokens(event):
                    confirmed = True
                    if on_output:
                        on_output()
        code = proc.poll()
        if code is not None:
            return 'complete', code
        if time.monotonic() - last_progress >= idle_seconds:
            stop_process_group(proc)
            return 'idle', proc.returncode
        time.sleep(poll_seconds)
