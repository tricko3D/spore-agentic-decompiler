#!/usr/bin/env python3
"""Live, read-only Inkling grid: per-slot decoded tok/s and aggregate tok/s.

Run this in a *different* WSL terminal while the calibration benchmark or
normal SAD workers are running. Reads llama.cpp /slots, never starts inference.
"""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_URL = "http://127.0.0.1:8080"
STATE_FILE = Path.home() / "inkling-runtime" / "sad-parallel-live.json"
RESULTS_FILE = Path.home() / "inkling-runtime" / "sad-parallel-benchmark.json"


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def poll_slots(url: str, timeout: float = 1.5):
    """The actual model's decode counters, not estimated text/token ratios."""
    request = Request(url.rstrip("/") + "/slots", headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        result = json.load(response)
    if not isinstance(result, list):
        raise ValueError("llama.cpp /slots returned a non-array response")
    return result


def decoded_count(slot):
    """Handle the llama.cpp next_token.n_decoded slot counter."""
    next_token = slot.get("next_token") or {}
    if isinstance(next_token, dict):
        count = next_token.get("n_decoded")
        if type(count) is int and count >= 0:
            return count
    count = slot.get("n_decoded")
    return count if type(count) is int and count >= 0 else None


class SlotRateTracker:
    def __init__(self, window_seconds: float = 3.0):
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.window = window_seconds
        self.samples = {}
        self.started = time.monotonic()

    def reset(self):
        self.samples.clear()

    def sample(self, slots, now: float | None = None):
        """Compute generated tokens / elapsed seconds over a rolling window.

        A slot's task changes or count resets when a request ends. Never count
        those transitions as newly generated tokens. Prompt prefill is excluded.
        """
        if now is None:
            now = time.monotonic()
        rows = []
        seen = set()
        for fallback_id, slot in enumerate(slots):
            if not isinstance(slot, dict):
                continue
            slot_id = slot.get("id", fallback_id)
            if not isinstance(slot_id, int):
                slot_id = fallback_id
            seen.add(slot_id)
            task = slot.get("id_task")
            busy = bool(slot.get("is_processing"))
            count = decoded_count(slot)
            context = slot.get("n_ctx")
            previous = self.samples.get(slot_id)
            new_request = (
                previous is None
                or not busy
                or not previous["busy"]
                or task != previous["task"]
                or count is None
                or previous["count"] is None
                or count < previous["count"]
                or now < previous["time"]
            )
            if new_request:
                first = now
                changes = deque()
            else:
                first = previous["first"]
                changes = previous["changes"]
                if count > previous["count"]:
                    changes.append((now, count - previous["count"]))
            while changes and now - changes[0][0] > self.window:
                changes.popleft()
            # The denominator grows from the first *observation* of this
            # request, capped to the same window used for the numerator.
            # Do not inflate rates while the model is only pre-filling.
            elapsed = min(self.window, now - first)
            speed = sum(n for _, n in changes) / elapsed if elapsed > 0 else 0.0
            if not busy:
                speed = 0.0
                state = "IDLE"
            elif speed > 0:
                state = "GEN"
            elif count is None or count == 0:
                state = "PREFILL"
            else:
                state = "WAIT"
            self.samples[slot_id] = {
                "task": task, "count": count, "time": now, "first": first,
                "changes": changes, "busy": busy,
            }
            rows.append({
                "id": slot_id,
                "rate": round(speed, 2),
                "status": state,
                "decoded": count,
                "context": context if type(context) is int else None,
                "task": task,
            })
        for missing in set(self.samples) - seen:
            del self.samples[missing]
        return sorted(rows, key=lambda x: x["id"])


def compact_number(n):
    if n is None:
        return "n/a"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1000:
        return f"{n/1000:.1f}K"
    return str(n)


def cell(row, width, compact=True):
    """Two rows per live slot, suitable even for 32-slot terminal grids."""
    inside = max(18, width - 2)
    index = f"#{row['id']+1:02d}"
    rate = f"{row['rate']:>5.1f} tok/s"
    state = row["status"]
    top = f" {index} {state:<7} {rate} "
    if len(top) > inside:
        top = f" {index} {state[:3]} {row['rate']:.1f} t/s "
    tokens = compact_number(row["decoded"])
    context = compact_number(row["context"])
    details = f" {tokens} decoded  {context} ctx "
    if len(details) > inside:
        details = f" {tokens} gen {context} ctx "
    pad_top = max(0, inside - len(top))
    pad_details = max(0, inside - len(details))
    return (
        "╭" + top + " "*pad_top + "╮",
        "╰" + details + " "*pad_details + "╯",
    )


def grid(rows, width=120):
    """Return terminal-size-aware grid with individual tok/s per card."""
    width = max(24, width)
    cols = max(1, min(8, width // 25))
    cols = min(cols, max(1, len(rows)))
    gutter = 1
    card_width = max(22, (width - gutter * (cols - 1)) // cols)
    lines = []
    for pos in range(0, len(rows), cols):
        group = rows[pos:pos + cols]
        rendered = [cell(row, card_width) for row in group]
        for line_idx in (0, 1):
            lines.append((" " * gutter).join(card[line_idx] for card in rendered))
    return lines


def benchmark_details(now_epoch: float | None = None):
    if now_epoch is None:
        now_epoch = time.time()
    state = read_json(STATE_FILE)
    status_line = "Model server live, SAD inference or benchmark"
    if isinstance(state, dict):
        age = now_epoch - state.get("updated_epoch", 0)
        if -10 < age < 30:
            stage = str(state.get("stage", "?")).upper()
            slots = state.get("workers")
            threads = state.get("threads")
            experts = state.get("cpu_expert_layers")
            round_no = state.get("round")
            status_line = (
                f"BENCHMARK {stage}  ·  {slots or '?'} slots  ·  "
                f"CPU threads {threads or '?'}  ·  CPU experts {experts or '?'}"
            )
            if round_no:
                status_line += f"  ·  round {round_no}"
    prior = read_json(RESULTS_FILE)
    best = None
    if isinstance(prior, dict):
        winner = prior.get("recommended")
        if isinstance(winner, dict):
            best = (
                f"Best measured: {winner['workers']} slots, "
                f"{winner['aggregate_output_tps']:.1f} aggregate tok/s  "
                f"({prior.get('status', 'partial')} sweep)"
            )
    return status_line, best


def render_dashboard(rows, width: int = 120, status: str = "",
                     details: str | None = None, error: str | None = None):
    active = sum(r["status"] != "IDLE" for r in rows)
    generating = sum(r["status"] == "GEN" for r in rows)
    total = sum(r["rate"] for r in rows)
    top = [
        "INKLING 2 MINI  |  LIVE INFERENCE".ljust(min(width, 65))
        + datetime.now().strftime("%H:%M:%S"),
        "",
        f"AGGREGATE  {total:,.1f} tok/s    |    "
        f"{generating} generating / {len(rows)} slots    |    "
        "3s decoded-token rolling window",
        f"{status}",
    ]
    if details:
        top.append(details)
    top.extend(["", "SLOTS  ·  Each cell shows live decoded tokens/sec  ·  Ctrl+C to close", ""])
    if rows:
        top.extend(grid(rows, width))
    elif error:
        top.append(f"Waiting for llama.cpp at {DEFAULT_URL}: {error}")
    else:
        top.append("Waiting for model server...")
    top.extend([
        "",
        "Live tok/s is sampled from llama.cpp /slots counters; prompt prefill excluded.",
        "Best measured tok/s comes from completed benchmark rounds, not the live sample.",
    ])
    return "\n".join(line[:max(width, 24)] for line in top) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=DEFAULT_URL,
                    help="llama.cpp server URL (default http://127.0.0.1:8080)")
    ap.add_argument("--refresh", type=float, default=0.5,
                    help="Dashboard refresh interval, seconds (default 0.5)")
    ap.add_argument("--once", action="store_true", help="Render once and exit")
    args = ap.parse_args(argv)
    if not 0.2 <= args.refresh <= 10:
        ap.error("--refresh must be between 0.2 and 10 seconds")
    tracker = SlotRateTracker()
    interactive = sys.stdout.isatty() and not args.once
    if interactive:
        sys.stdout.write("\033[?1049h\033[?25l")
        sys.stdout.flush()
    try:
        while True:
            width = shutil.get_terminal_size((120, 24)).columns
            error = None
            try:
                slots = poll_slots(args.url)
                rows = tracker.sample(slots)
            except (OSError, HTTPError, URLError, ValueError, TimeoutError) as exc:
                error = f"{type(exc).__name__}: {str(exc)[:100]}"
                tracker.reset()
                rows = []
            stage, best = benchmark_details()
            screen = render_dashboard(rows, width, stage, best, error)
            if interactive:
                sys.stdout.write("\033[H\033[J" + screen)
            else:
                sys.stdout.write(screen)
            sys.stdout.flush()
            if args.once:
                return 0
            time.sleep(args.refresh)
    except KeyboardInterrupt:
        return 0
    finally:
        if interactive:
            sys.stdout.write("\033[?25h\033[?1049l")
            sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
