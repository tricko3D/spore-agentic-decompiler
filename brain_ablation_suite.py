#!/usr/bin/env python3
"""One-command, resumable, randomized solved-function Brain comparison.

Four experimental arms × three known-solvable functions × three repetitions.
Offline preparation by default; model spend requires the explicit --execute flag.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
from pathlib import Path
import random

import brain_ablation as brain

TARGET_CANDIDATES=("0x00c45760","0x00c2f1f0","0x00b8d190",
                   "0x00dba2f0","0x00c9ae30","0x00c9aa10")
MAX_TARGETS=3
SUITE_VERSION=1


def write(path,data):
    brain.write_json(path,data)


def _load(out):
    path=out/"suite-plan.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _target_dir(out,va):
    return out/"functions"/va[2:]


def _eligible_solved_sources(repo):
    """Predeclared history list, filtered by actual source and manifest safety."""
    from collections import OrderedDict
    history={c["va"]:c for c in brain.known_solutions(repo,minimum=3)}
    included=[]
    rejected={}
    manifests=set()
    for path in brain._manifest_files(repo):
        for line in path.read_text(encoding="utf-8",errors="replace").splitlines():
            fields=line.split("#",1)[0].split()
            if len(fields)>2 and len(fields[2])==8:
                try:
                    manifests.add("0x"+f"{int(fields[2],16):08x}")
                except ValueError:
                    continue
    for va in TARGET_CANDIDATES:
        record=history.get(va)
        source=repo/brain.slice_path(va)
        if not record:
            rejected[va]="No accepted MATCH after >=3 failed attempts, or missing dedicated slice"
            continue
        if va not in manifests:
            rejected[va]="No independently registered MATCH manifest entry"
            continue
        if not source.is_file() or source.is_symlink():
            rejected[va]="Source is absent or symlinked"
            continue
        import re
        markers=[x.lower() for x in re.findall(
            r"(?mi)^\s*//\s*@\s*(0x[0-9a-f]{8})",
            source.read_text(encoding="utf-8",errors="replace"))]
        if markers!=[va]:
            rejected[va]="Not an isolated one-function source marker (found "+str(markers[:8])+")"
            continue
        included.append(va)
    return included[:MAX_TARGETS],rejected,history


def prepare(repo,out,repeats=3,seed=20261118):
    repo=repo.resolve()
    out=out.resolve()
    if out.is_relative_to(repo) or repo.is_relative_to(out):
        raise ValueError("Experiment output must not contain, or live inside, the upstream checkout")
    if not 1<=repeats<=10:
        raise ValueError("repeats must be 1..10")
    plan=_load(out)
    if plan is None:
        if out.exists() and any(out.iterdir()):
            raise FileExistsError("Nonempty output has no suite-plan.json; refusing to overwrite")
        out.mkdir(parents=True,exist_ok=True)
        targets,rejected,found=_eligible_solved_sources(repo)
        if len(targets)!=MAX_TARGETS:
            message=" ; ".join(f"{va}: {reason}" for va,reason in rejected.items())
            raise RuntimeError(
                f"Only {len(targets)} of {MAX_TARGETS} required independent solved "
                "functions qualify. No paid work started. "+message)
        rng=random.Random(seed)
        # Randomize which function goes first in each block of three,
        # preserving the randomized within-function arm order.
        schedule=[]
        for repetition in range(4*repeats):
            trio=list(targets)
            rng.shuffle(trio)
            schedule+=trio
        plan={
            "suite_version":SUITE_VERSION,
            "benchmark_protocol":brain.PROTOCOL_VERSION,
            "created":brain.now(),
            "source":str(repo),
            "targets":list(targets),
            "excluded_candidates":rejected,
            "historical_failures_before_match":{va:found[va]["failures"] for va in targets},
            "repeats":repeats,
            "random_seed":seed,
            "schedule":schedule,
            "model":"deepseek/deepseek-v4.1-flash-1017",
            "notes":"Frozen solved-function cohort, no original solution is included in a trial."
        }
        write(out/"suite-plan.json",plan)
    if (plan.get("suite_version")!=SUITE_VERSION or
            plan.get("benchmark_protocol")!=brain.PROTOCOL_VERSION or
            plan.get("source")!=str(repo) or
            (not plan.get("targets") or
             not set(plan["targets"])<=set(TARGET_CANDIDATES) or
             len(plan["targets"])!=MAX_TARGETS) or
            plan.get("repeats")!=repeats or
            plan.get("random_seed")!=seed):
        raise RuntimeError("Suite configuration changed. Refusing to mix experimental protocols")
    targets=tuple(plan["targets"])
    if len(plan["schedule"])!=len(targets)*4*repeats:
        raise RuntimeError("Invalid randomized suite schedule")
    frozen=out/"frozen"/"knowledge.sqlite3"
    previous_any=any((_target_dir(out,va)/"plan.json").exists()
                     for va in targets)
    if not frozen.is_file():
        if previous_any:
            raise RuntimeError("Frozen source knowledge DB missing after target preparation")
        frozen.parent.mkdir(parents=True,exist_ok=True)
        brain.snapshot_db(repo/brain.KNOW,frozen)
    frozen_sha=brain._tool_hash(frozen)
    if "frozen_knowledge_sha256" in plan:
        if plan["frozen_knowledge_sha256"]!=frozen_sha:
            raise RuntimeError("Frozen suite knowledge changed; refusing mixed treatments")
    else:
        if previous_any:
            raise RuntimeError("Cannot add knowledge fingerprint after trial preparation")
        plan["frozen_knowledge_sha256"]=frozen_sha
        write(out/"suite-plan.json",plan)
    for i,va in enumerate(targets):
        sub=_target_dir(out,va)
        if not (sub/"plan.json").is_file():
            if sub.exists():
                raise RuntimeError(f"Partial target preparation in {sub}; preserve for inspection")
            print(f"Preparing heldout {va} ({i+1}/{len(targets)})...",flush=True)
            brain.prepare(repo,sub,va=va,repeats=repeats,minimum=3,
                          seed=seed+i,solved=True,holdouts=targets,
                          frozen_knowledge=frozen)
        inner=json.loads((sub/"plan.json").read_text(encoding="utf-8"))
        if (not inner.get("solved_cohort") or
                tuple(inner.get("holdouts") or ())!=tuple(sorted(targets)) or
                inner.get("target")!=va or inner.get("repeats")!=repeats or
                inner.get("source_knowledge_sha256")!=frozen_sha):
            raise RuntimeError(f"Target {va} does not have a validated solved-cohort protocol")
        brain._seed_tooling(sub,inner)
    return plan


def _target_rows(out,va):
    sub=_target_dir(out,va)
    with contextlib.redirect_stdout(io.StringIO()):
        brain.report(sub)
    report=json.loads((sub/"report.json").read_text(encoding="utf-8"))
    if report["target"]!=va:
        raise RuntimeError("Mismatched target report: "+va)
    rows=[]
    for r in report["trials"]:
        val={**r,"target":va}
        val["valid"]=(r["state"]=="completed" and r.get("protocol_valid") is True
                      and r["match"] is not None)
        rows.append(val)
    return rows


def compare(out):
    out=out.resolve()
    plan=_load(out)
    if not plan:
        raise RuntimeError("No suite-plan.json. Run the suite first")
    rows=[]
    for va in plan["targets"]:
        if not (_target_dir(out,va)/"plan.json").is_file():
            continue
        rows.extend(_target_rows(out,va))
    order={a:i for i,a in enumerate(brain.ARMS)}
    rows.sort(key=lambda r:(plan["targets"].index(r["target"]),
                           order[r["arm"]],r["repeat"]))
    doc={"suite_version":SUITE_VERSION,"generated":brain.now(),
         "targets":plan["targets"],"repeats":plan["repeats"],
         "primary_endpoint":"independently verified byte-perfect MATCH",
         "trials":rows}
    write(out/"suite-report.json",doc)
    cols=["target","arm","repeat","state","status","match","valid",
          "checker_calls","brain_queries","findings_returned",
          "techniques_returned","treatment_adherent","input_tokens",
          "output_tokens","cost_usd","result_note","name"]
    with (out/"suite-results.csv").open("w",newline="",encoding="utf-8") as f:
        wr=csv.DictWriter(f,fieldnames=cols,extrasaction="ignore")
        wr.writeheader()
        wr.writerows(rows)
    print(f"\nKNOWN-SOLVABLE SUITE | {len(rows)}/{len(plan['targets'])*4*plan['repeats']} prepared trials")
    print("Function       "+"  ".join(f"{arm:>13}" for arm in brain.ARMS))
    for va in plan["targets"]:
        cells=[]
        for arm in brain.ARMS:
            active=[r for r in rows if r["target"]==va and r["arm"]==arm and r["valid"]]
            wins=sum(bool(r["match"]) for r in active)
            cells.append(f"{wins}/{len(active)} MATCH")
        print(f"{va:<14}"+"  ".join(f"{v:>13}" for v in cells))
    print("\nOverall primary endpoint:")
    for arm in brain.ARMS:
        group=[r for r in rows if r["arm"]==arm]
        measured=[r for r in group if r["valid"]]
        wins=sum(bool(r["match"]) for r in measured)
        adherent=sum(bool(r.get("treatment_adherent")) for r in measured)
        print(f"  {arm:<13} MATCH {wins}/{len(measured)}, "
              f"measured {len(measured)}/{len(plan['targets'])*plan['repeats']}, "
              f"treatment exposed {adherent}/{len(measured)}")
        successful=[r for r in measured if r["match"]]
        if successful:
            mean_tokens=sum((r.get("input_tokens") or 0)+(r.get("output_tokens") or 0)
                            for r in successful)/len(successful)
            mean_checks=sum(r.get("checker_calls") or 0 for r in successful)/len(successful)
            print(f"    Among MATCH only: {mean_tokens:,.0f} tokens, {mean_checks:.1f} checker calls on average")
    bad=[r for r in rows if r["state"] in ("error","blocked","interrupted","protocol_invalid")]
    if bad:
        print("  BLOCKED/INVALID trials:",len(bad),
              "; NOT counted as MATCH failures (investigate before resuming)")
    print("Unsolved runs are not counted as evidence of improved quality merely for using fewer tokens.")
    print("Trial evidence: suite-results.csv and suite-report.json")
    return doc


def run(repo,out,repeats=3,seed=20261118,*,execute=False,budget=.50,minutes=20):
    if not .10<=budget<=25 or not 1<=minutes<=180:
        raise ValueError("Trial budget must be $0.10-$25; inactivity minutes 1-180")
    plan=prepare(repo,out,repeats,seed)
    total=len(plan["schedule"])
    print(f"Suite ready: {len(plan['targets'])} known MATCH functions x 4 arms x "
          f"{repeats} repetitions = {total} trials")
    print(f"Per-trial spending guard: USD {budget:.2f}; "
          f"nominal sum of per-trial budgets: USD {total*budget:.2f}")
    if not execute:
        print("DRY RUN ONLY, no agent inference. Add --execute to run every trial.")
        return compare(out)
    remaining={va:list(json.loads((_target_dir(out,va)/"plan.json").read_text())
                        ["arms"]) for va in plan["targets"]}
    # Each target plan is pre-randomized. The suite schedule interleaves target
    # blocks, ensuring no arm consistently runs first across all functions.
    for n,va in enumerate(plan["schedule"],start=1):
        current=remaining[va].pop(0)
        target_out=_target_dir(out,va)
        state=json.loads((target_out/"plan.json").read_text())["arms"]
        latest=next((item for item in state
                     if item["arm"]==current["arm"] and item["repeat"]==current["repeat"]),None)
        if latest is None:
            raise RuntimeError("Scheduled arm vanished from immutable target plan")
        if latest["state"]=="completed":
            continue
        if latest["state"]!="prepared":
            raise RuntimeError(f"Trial {va}/{current['arm']}/{current['repeat']} "
                               f"is {latest['state']}, manual audit required")
        first=next((item for item in state if item["state"]=="prepared"),None)
        if first is None or (first["arm"],first["repeat"])!=(current["arm"],current["repeat"]):
            raise RuntimeError("Per-target randomized plan differs from suite schedule")
        print(f"\n[{n}/{total}] {va} | {current['arm']} repetition {current['repeat']}",
              flush=True)
        brain.run_next(target_out,execute=True,budget=budget,minutes=minutes)
        fresh=json.loads((target_out/"plan.json").read_text())["arms"]
        nowitem=next(i for i in fresh if i["arm"]==current["arm"]
                     and i["repeat"]==current["repeat"])
        if nowitem["state"]!="completed":
            compare(out)
            raise RuntimeError(f"Stopped on {va} {current['arm']}: "
                               f"{nowitem['state']}; inspect target trial logs")
        compare(out)
    print("\nAll scheduled trials completed.")
    return compare(out)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    cmds=p.add_subparsers(dest="command",required=True)
    r=cmds.add_parser("run",help="Prepare and optionally execute the entire randomized suite")
    r.add_argument("--repo",type=Path,default=Path.home()/"spore-decomp")
    r.add_argument("--out",type=Path,default=Path.home()/"sad-known-solved-suite")
    r.add_argument("--repeats",type=int,default=3)
    r.add_argument("--seed",type=int,default=20261118)
    r.add_argument("--execute",action="store_true",help="Purchase inference for all prepared trials")
    r.add_argument("--budget",type=float,default=.5,help="OpenRouter per-trial soft limit, USD")
    r.add_argument("--minutes",type=int,default=20)
    c=cmds.add_parser("compare",help="Compare recorded results without model calls")
    c.add_argument("--out",type=Path,default=Path.home()/"sad-known-solved-suite")
    a=p.parse_args(argv)
    if a.command=="compare":
        compare(a.out)
    else:
        run(a.repo,a.out,a.repeats,a.seed,execute=a.execute,
            budget=a.budget,minutes=a.minutes)


if __name__=="__main__":
    main()
