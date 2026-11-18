"""Evidence extraction for SAD Collective Memory (stdlib only).

Records only observed checker executions/results. No attempt is made to infer
which code change caused an improvement unless supported by source snapshots.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

EXEC = re.compile(
    r"(?:^|[;&|\n(]\s*|(?:\s+))"
    r"(?:\S*/)?python(?:3(?:\.\d+)?)?\s+"
    r"(?:-u\s+)?(?:\./)?tools/contrib/check\.py\s+"
    r"(0x[0-9a-f]{8})\b", re.I
)
FLAGS = re.compile(r"\[([^\]\n]{2,160})\]")
OVERRIDE = re.compile(r"--flags\s+(?:['\"]([^'\"]+)['\"]|([^\s;&|]+))")
SIZE = re.compile(r"\bsize\s*[:=]\s*(\d+)\s*/\s*(\d+)", re.I)
SIZE_RETAIL = re.compile(r"\bsize\s+(\d+)\s*,\s*retail\s+(\d+)", re.I)
DIFFS = re.compile(r"\b(?:diffs?|differences)\s*[:=]\s*(\d+)\b", re.I)
OFFSETS = re.compile(r"\boffsets?\s*[:=]\s*([0-9a-fx,\s]+)", re.I)
INLINE_DUPLICATE = re.compile(r"^(.*?)\s+\1$", re.I | re.S)


def _flat(value, limit=900):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def concise_summary(note, checker, limit=800):
    """Do not combine the same checker/message text twice, including old notes."""
    obs = _flat(note, limit)
    verdict = _flat(checker, limit)
    # The operational runner appends '| checker: ...' to its attempt note.
    match = re.search(r"\s*\|\s*checker\s*:", obs, flags=re.I)
    if match:
        obs = obs[:match.start()].strip()
    obs = re.sub(r"^Pass\s+\d+\s*:\s*", "", obs, flags=re.I)
    obs = re.sub(r"^FAIL\s*\+\s*[1-5]\s*:\s*", "", obs, flags=re.I)
    if obs.lower() == verdict.lower():
        return obs[:limit]
    # Many old rows encode both fields as the same phrase, or one contains the
    # other. Never repeat an observation just to consume context.
    if obs and verdict and (obs.casefold() in verdict.casefold()):
        return verdict[:limit]
    if obs and verdict and (verdict.casefold() in obs.casefold()):
        return obs[:limit]
    parts = list(dict.fromkeys(item for item in (obs, verdict) if item))
    return " | ".join(parts)[:limit]


def parse_experiments(log_path, va, root, *, max_bytes=35_000_000, max_events=250):
    """Extract actual checker tool results from an immutable JSONL log.

    Reading check.py source is NOT an experiment. Results and compiler flags
    are tool-observed. They may lack comparable byte metrics, which remain
    null rather than guessed. Large logs and partial JSONL lines are safe.
    """
    try:
        root = Path(root).expanduser().resolve()
        p = Path(log_path)
        p = (p if p.is_absolute() else root / p).resolve()
        allowed = root / "work/scratch/codex-pass1/logs"
        if not p.is_relative_to(allowed) or not p.is_file():
            return []
    except (OSError, ValueError, TypeError):
        return []
    records = []
    seen = set()
    try:
        with p.open("r", encoding="utf-8", errors="replace") as inp:
            processed = 0
            for line in inp:
                processed += len(line.encode("utf-8", "replace"))
                if processed > max_bytes:
                    break
                if '"tool_use"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, dict) or event.get("type") != "tool_use":
                    continue
                part = event.get("part") or {}
                state = part.get("state") or {}
                if state.get("status") not in (None, "completed"):
                    continue
                inputs = state.get("input") or {}
                command = str(inputs.get("command") or inputs.get("cmd") or "")
                match = EXEC.search(command)
                if not match or match.group(1).lower() != va.lower():
                    continue
                output = state.get("output")
                if not isinstance(output, str) or not output.strip():
                    continue
                fingerprint = (command, output)
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                verdict = next((l.strip() for l in output.splitlines()
                                if l.strip().startswith(("MATCH ", "NO MATCH ",
                                                         "COMPILE FAILED"))), "")
                size = SIZE.search(output) or SIZE_RETAIL.search(output)
                diff = DIFFS.search(output)
                offsets = OFFSETS.search(output)
                flags = OVERRIDE.search(command)
                if flags:
                    flags = flags.group(1) or flags.group(2)
                else:
                    f = FLAGS.search(verdict)
                    flags = f.group(1) if f else ""
                # The checker headline alone usually says only NO MATCH.
                # Capture its concrete second-line reason, when present.
                reason = next((v.strip() for v in output.splitlines()[1:6]
                               if re.search(r"^\s*(?:bytes differ|size \d+, retail \d+|"
                                            r"relocation at offset|COMPILE FAILED)", v, re.I)), "")
                summary = _flat((verdict or output.strip().splitlines()[0]) +
                                ((" | " + reason) if reason else ""), 380)
                records.append({
                    "ordinal": len(records) + 1,
                    "command": _flat(command, 450),
                    "flags": _flat(flags, 180),
                    "size_compiled": int(size.group(1)) if size else None,
                    "size_retail": int(size.group(2)) if size else None,
                    "diff_bytes": int(diff.group(1)) if diff else None,
                    "offsets": _flat(offsets.group(1), 180) if offsets else "",
                    "result": summary,
                })
                if len(records) >= max_events:
                    break
    except (OSError, UnicodeError):
        return []
    return records


def experiment_digest(records):
    return json.dumps(records, sort_keys=True, separators=(",", ":"))


def experiment_brief(records, *, max_items=6, max_chars=1150):
    """Terse, verifiable chronology with measured progress, not speculation."""
    if not records:
        return ""
    recent = records[-max_items:]
    lines = []
    previous = None
    for r in records:
        if r["diff_bytes"] is not None and r["size_retail"] is not None:
            if previous and previous["size_retail"] == r["size_retail"]:
                r["comparison"] = ("improved" if r["diff_bytes"] < previous["diff_bytes"]
                                   else "worsened" if r["diff_bytes"] > previous["diff_bytes"]
                                   else "unchanged")
            previous = r
    for r in recent:
        metrics = ""
        if r["size_compiled"] is not None and r["size_retail"] is not None:
            metrics = f"size {r['size_compiled']}/{r['size_retail']}"
        if r["diff_bytes"] is not None:
            metrics += ("; " if metrics else "") + f"{r['diff_bytes']} differing bytes"
        if r.get("comparison"):
            metrics += f" ({r['comparison']} vs prior recorded check)"
        label = " ; ".join(x for x in (r["flags"], metrics, r["result"][:180]) if x)
        lines.append(f"check {r['ordinal']}: {label}")
    return "\n".join(lines)[:max_chars]
