#!/usr/bin/env python3
"""Research-only Brain access for the four-arm Spore ablation experiment.

The agent queries this tool at runtime. Relevant hits are never prepared in
advance. Arm assignments come from the cloned experiment workspace, not from
agent-supplied arguments. No paid model calls occur here.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

# The installed upstream agent modules live beside the copied query tool.
import spore_collective_memory as memory

CONFIG = Path("work/scratch/codex-pass1/brain_trial.json")
LOG = Path("work/scratch/codex-pass1/brain-queries.jsonl")
BUDGET = 3000


def config():
    value = json.loads(CONFIG.read_text(encoding="utf-8"))
    if value.get("arm") not in {"baseline", "raw", "consolidated", "placebo"}:
        raise ValueError("Invalid experiment arm")
    return value


def scrub(text, cfg):
    target = cfg["target"].lower()
    return re.sub(re.escape(target), "[TARGET]", str(text), flags=re.I)


def _candidate_score(text, document):
    words = set(re.findall(r"[a-z][a-z0-9_]{4,}", text.lower()))
    words -= {"about", "compiler", "function", "spore", "retail", "failed", "reason", "could", "which"}
    content = document.lower()
    return sum(w in content for w in words)


def _sample(pool, text, seed, top, field):
    # Deterministic, low-overlap real evidence instead of fabricated nonsense.
    pool = [x for x in pool if _candidate_score(text, str(x.get(field, ""))) == 0]
    pool.sort(key=lambda x: hashlib.sha256(
        (str(seed) + "|" + text + "|" + str(x.get("id"))).encode()).digest())
    return pool[:top]


def distractors(repo, text, seed):
    con = sqlite3.connect(memory.paths(repo)[2])
    con.row_factory = sqlite3.Row
    try:
        findings = []
        for row in con.execute("""SELECT id,va,pass_number,status,verified_match,summary,
            evidence_path FROM findings ORDER BY id LIMIT 1200"""):
            r = dict(row)
            if r["va"].lower()==os.environ.get("SAD_BENCH_TARGET_ALIAS","").lower():
                continue
            findings.append({"id":r["id"], "va":r["va"], "pass":r["pass_number"],
                 "status":r["status"], "verified_match":bool(r["verified_match"]),
                 "summary":r["summary"], "experiments":[], "evidence_path":"",
                 "score":0})
        techniques = []
        try:
            rows = con.execute("""SELECT id,title,category,problem,conditions,
              intervention,observed_effect,evidence_level FROM compiler_techniques
              ORDER BY id LIMIT 500""")
            for row in rows:
                techniques.append(dict(row) | {"sources":[], "score":0})
        except sqlite3.OperationalError:
            pass
        return (_sample(findings,text,seed,15,"summary"),
                _sample(techniques,text,seed,10,"problem"))
    finally:
        con.close()


def lookup(repo, cfg, text):
    arm = cfg["arm"]
    os.environ["SAD_BENCH_TARGET_ALIAS"]=cfg["alias"]
    if arm == "baseline":
        return "Knowledge unavailable in this control run.", 0, 0
    if cfg["target"].lower() in text.lower():
        return "Direct target-address lookup is disabled in this experiment. Use a technical question.", 0, 0
    if arm == "raw":
        items = memory.query(repo,text,limit=8)
        result = "ORIGINAL FINDINGS (unverified unless independently matched):\n"
        result += memory.print_findings(items,max_chars=BUDGET-130)
        return scrub(result[:BUDGET],cfg), len(items), 0
    if arm == "consolidated":
        findings = memory.query(repo,text,limit=4)
        techniques = memory.technique_query(repo,text,limit=4)
        result = ("ORIGINAL FINDINGS:\n" +
                  memory.print_findings(findings,max_chars=1220) +
                  "\n\nCONSOLIDATED COMPILER TECHNIQUES (not established rules):\n" +
                  memory.print_techniques(techniques,max_chars=1650))
        return scrub(result[:BUDGET],cfg),len(findings),len(techniques)
    # Calibrate the placebo's amount of context against genuine retrieval
    # for THIS agent-generated query. Use unrelated *real* stored evidence.
    findings = memory.query(repo,text,limit=4)
    techniques = memory.technique_query(repo,text,limit=4)
    reference = ("ORIGINAL FINDINGS:\n" + memory.print_findings(findings,max_chars=1220)
                 + "\n\nCONSOLIDATED COMPILER TECHNIQUES:\n" +
                 memory.print_techniques(techniques,max_chars=1650))
    size = min(BUDGET,max(250,len(reference)))
    other_findings,other_techniques = distractors(repo,text,cfg["seed"])
    result = ("ORIGINAL FINDINGS:\n" +
              memory.print_findings(other_findings[:9],max_chars=1250) +
              "\n\nCOMPILER TECHNIQUES:\n" +
              memory.print_techniques(other_techniques[:7],max_chars=1650))
    if len(result) < size:
        # Add more real unrelated findings, never synthetic technical claims.
        result += "\n\nADDITIONAL FINDINGS:\n" + memory.print_findings(
            other_findings[9:],max_chars=size-len(result))
    if len(result) < size * 0.65:
        raise RuntimeError("Insufficient unrelated evidence for a length-matched placebo")
    return scrub(result[:size],cfg),len(other_findings),len(other_techniques)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--text",required=True,help="Technical question, not a function address")
    a = p.parse_args(argv)
    cfg = config()
    result,nfind,ntech = lookup(Path.cwd(),cfg,a.text[:1800])
    # Query audit exists only in this experiment copy and contains no API key.
    LOG.parent.mkdir(parents=True,exist_ok=True)
    with LOG.open("a",encoding="utf-8") as handle:
        handle.write(json.dumps({
            "utc":datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "arm":cfg["arm"],"question":a.text[:1800],
            "characters":len(result),"findings":nfind,"techniques":ntech
        },ensure_ascii=False)+"\n")
    print(result)


if __name__ == "__main__":
    main()
