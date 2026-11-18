#!/usr/bin/env python3
"""Reproducible four-arm Spore decompilation ablation benchmark.

Preparation is strictly offline and only reads the live Spore checkout.
The paid runner is NEVER started without the explicit run-next --execute flag.
Each arm uses a separate full working tree, SQLite database, session and source.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time

ARMS = ("baseline", "raw", "consolidated", "placebo")
WORK = Path("work/scratch/codex-pass1")
OPS = WORK / "spore.sqlite3"
KNOW = WORK / "memory/knowledge.sqlite3"
RUNNER = Path("work/scratch/spore_first_pass_sqlite.py")
MEMORY = Path("work/scratch/spore_collective_memory.py")
RETRIEVAL = Path("work/scratch/brain_trial_query.py")
CONFIG = WORK / "brain_trial.json"
SOURCE_DIR = Path(__file__).resolve().parent
RETRIEVAL_SOURCE = SOURCE_DIR / "brain_ablation_query.py"
ENTRY = Path("brain_trial_entry.py")
ENTRY_SOURCE = SOURCE_DIR / ENTRY.name
MEMORY_TEMPLATE = SOURCE_DIR / "engine/spore_provider_fix/work/scratch/spore_collective_memory.py"
SOCKET_MARKER = "SAD_SHORT_UNIX_SOCKET_V1"
DEFAULT_BUDGET = 0.50


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+".tmp")
    tmp.write_text(json.dumps(data,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    tmp.replace(path)


def snapshot_db(source, destination):
    """SQLite online backup, safe even when WAL writers are active."""
    if not source.is_file():
        raise FileNotFoundError(f"Missing required SQLite database: {source}")
    destination.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(f"file:{source.resolve()}?mode=ro",uri=True) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
            dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def canonical(va):
    if va is None:
        return None
    if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{8}",va):
        raise ValueError("Function address must contain eight hexadecimal digits")
    return "0x"+va.lower().removeprefix("0x")


def slice_path(va):
    return Path("match/slices")/("s"+va[2:])/("s"+va[2:]+".cpp")


def _checked_history(db):
    return db.execute("""SELECT q.va,q.size,
           SUM(CASE WHEN a.status IN ('FAIL','DEFER') AND v.attempt_id IS NULL
                    THEN 1 ELSE 0 END) AS failures,
           MAX(CASE WHEN a.status='MATCH' AND v.attempt_id IS NULL
                    THEN 1 ELSE 0 END) AS matched
       FROM queue q LEFT JOIN attempts a ON a.va=q.va
       LEFT JOIN attempt_reviews v ON v.attempt_id=a.id
       GROUP BY q.va,q.size ORDER BY failures DESC,q.va""").fetchall()


def candidates(repo, minimum=7):
    with sqlite3.connect(f"file:{(repo/OPS).resolve()}?mode=ro",uri=True) as db:
        rows = _checked_history(db)
    return [{"va":va,"size":size,"failures":n,"source":str(slice_path(va))}
            for va,size,n,matched in rows if n>=minimum and not matched
            and (repo/slice_path(va)).is_file()]


def _copy_ignore(base):
    """Do not copy live SQLite/WAL, sockets, old sessions or past tool logs."""
    def ignored(dirname,names):
        path=Path(dirname)
        rel=path.relative_to(base)
        drop=set()
        if rel==Path("."):
            drop |= {".git",".venv","node_modules","__pycache__",".pytest_cache"}
        if rel==WORK:
            drop |= {"spore.sqlite3","spore.sqlite3-wal","spore.sqlite3-shm",
                     "logs","investigations","parallel","runtime","sqlite-backups"}
        if rel==WORK/"memory":
            drop |= {"knowledge.sqlite3","knowledge.sqlite3-wal",
                     "knowledge.sqlite3-shm","memory.sock",".venv"}
        for name in names:
            p=path/name
            if name=="__pycache__" or name==".DS_Store":
                drop.add(name)
            elif p.is_socket() or (not p.is_symlink() and not p.is_file() and not p.is_dir()):
                drop.add(name)
        return drop
    return ignored


def _relocate_symlinks(tree, source):
    """Make copied in-repo links resolve inside their new checkout, not into source.

    Both the seed and every trial need this. Rewriting seed links alone is
    insufficient when a copied trial contains an absolute link back to seed.
    External links are not followed during copying and are left unchanged.
    """
    tree=tree.resolve()
    source=source.resolve()
    remapped=0
    for path in tree.rglob("*"):
        if not path.is_symlink():
            continue
        if path.relative_to(tree) in {Path(".venv"), WORK/"memory/.venv"}:
            # Explicitly shared, installed runtime dependencies.
            continue
        try:
            target=path.resolve(strict=False)
        except (OSError,RuntimeError) as exc:
            raise RuntimeError(f"Invalid symlink in benchmark copy: {path}") from exc
        if not target.is_relative_to(source):
            continue
        local=tree/target.relative_to(source)
        if not local.exists():
            raise RuntimeError(
                f"Cannot isolate symlink {path.relative_to(tree)}: copied target "
                f"{local.relative_to(tree)} is missing (possibly ignored during copy). "
                "No experiment was started.")
        relative=os.path.relpath(local,start=path.parent)
        directory=local.is_dir()
        path.unlink()  # Never modify the original referent.
        path.symlink_to(relative,target_is_directory=directory)
        remapped+=1
    # Fail closed if any link was unexpectedly left pointing into source.
    for path in tree.rglob("*"):
        if path.is_symlink() and path.relative_to(tree) not in {
                Path(".venv"), WORK/"memory/.venv"}:
            if path.resolve(strict=False).is_relative_to(source):
                raise RuntimeError(f"Unisolated source symlink: {path}")
    return remapped


def _validate_partial(repo, out, va, original):
    """Resume only the exact pre-snapshot-copy failure state of prepare()."""
    archive=out/"archive"
    seed_tree=out/"seed"
    required=[
        archive/"spore.sqlite3", archive/"knowledge.sqlite3",
        archive/"original-target.cpp", archive/"original-target.sha256",
        archive/"target-attempts.json", seed_tree/RUNNER,
        seed_tree/MEMORY, seed_tree/slice_path(va)
    ]
    if any(not p.is_file() for p in required):
        raise RuntimeError("Partial preparation is missing required backups or seed files; "
                           "keep this directory and start a new output directory")
    if (out/"plan.json").exists() or (out/"trials").exists():
        raise RuntimeError("Refusing to resume a finalized or trial-bearing experiment")
    if (seed_tree/OPS).exists() or (seed_tree/KNOW).exists():
        raise RuntimeError("Cannot resume after trial database installation; use a new directory")
    digest=hashlib.sha256(original.encode()).hexdigest()
    if archive.joinpath("original-target.sha256").read_text().strip()!=digest:
        raise RuntimeError("Live target changed since the backup; refusing to mix source versions")
    if archive.joinpath("original-target.cpp").read_text()!=original:
        raise RuntimeError("Archived C++ differs from the live target")
    if (seed_tree/slice_path(va)).read_text()!=original:
        raise RuntimeError("Seed target differs from the archived C++; refusing unsafe resume")
    if (seed_tree/RUNNER).read_bytes()!=(repo/RUNNER).read_bytes():
        raise RuntimeError("Runner changed after failed copy; start a new experiment")
    # Ensure the two WAL-consistent backups can be opened and contain the target.
    with sqlite3.connect(f"file:{(archive/'spore.sqlite3').resolve()}?mode=ro",uri=True) as db:
        if not db.execute("SELECT 1 FROM attempts WHERE va=? LIMIT 1",(va,)).fetchone():
            raise RuntimeError("Partial operational backup lacks the target attempts")
    with sqlite3.connect(f"file:{(archive/'knowledge.sqlite3').resolve()}?mode=ro",uri=True) as db:
        db.execute("SELECT 1 FROM findings LIMIT 1").fetchone()
    return seed_tree


def _patch_runner(path):
    source=path.read_text(encoding="utf-8")
    needle="if collective_memory.paths(ROOT)[2].is_file():"
    if source.count(needle)!=1:
        raise RuntimeError("Cannot locate memory injection boundary; runner version changed")
    source=source.replace(needle,
             "if not os.environ.get('SAD_BENCH_ACTIVE') and collective_memory.paths(ROOT)[2].is_file():")
    anchor="    if os.environ.get('SPORE_SQLITE_RUN_ID'):\n        prompt += f\"\"\""
    if source.count(anchor)!=1:
        raise RuntimeError("Cannot locate agent prompt boundary; runner version changed")
    insert='''    if os.environ.get('SAD_BENCH_ACTIVE'):
        prompt += """
CONTROLLED RESEARCH EXPERIMENT: Start this function from its blank source slice.
Before proposing your first compiler hypothesis, query the Brain with a specific
technical question using:
.venv/bin/python brain_trial_entry.py query --text "MSVC x86 stack frame and register allocation"
You may query the Brain repeatedly using your own technical questions.
Some control conditions return no useful knowledge. Treat this as normal.
Never look up knowledge by the target function address. Do not inspect
historical logs, prior solutions, SQLite files, benchmark setup files,
other sandboxes or agent sessions. Use only the benchmark Brain query tool.
Consult task.py and check.py normally. Never trust a model claim of MATCH;
only the retail checker proves byte-exact agreement.
Focus on the current function. Do not edit unrelated files.
"""
'''
    source=source.replace(anchor,insert+anchor)
    path.write_text(source,encoding="utf-8")


def _patch_memory_socket(path):
    """Apply only the tested socket-path compatibility shim to the cloned Brain.

    Never rewrite the live checkout. Preserve the installed memory retrieval
    implementation, schema and embedding behavior. The fix is idempotent.
    """
    code=path.read_text(encoding="utf-8")
    template=MEMORY_TEMPLATE.read_text(encoding="utf-8")
    if SOCKET_MARKER not in template:
        raise RuntimeError("SAD template does not contain the short-socket fix")
    old_paths='''def paths(repo):
    root = Path(repo).expanduser().resolve()
    folder = root / "work/scratch/codex-pass1/memory"
    return root, folder, folder / "knowledge.sqlite3", folder / "memory.sock"
'''
    old_serve='''def serve(repo):
    _, folder, _, sock = paths(repo)
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    # Refuse to steal a live service's socket.'''
    def section(s, start, end):
        left=s.index(start)
        right=s.index(end,left)
        return s[left:right]
    patched_paths=section(template,"def paths(repo):","def utc():")
    patched_serve=section(template,"def serve(repo):",
                           "    # Refuse to steal a live service's socket.")
    if SOCKET_MARKER in code:
        if ("Unsafe private memory socket directory" not in code
                or "import stat" not in code):
            raise RuntimeError("Memory socket shim is partially installed")
        return False
    if code.count(old_paths)!=1 or code.count(old_serve)!=1:
        raise RuntimeError("Unexpected Collective Memory version: cannot safely patch Unix socket")
    if code.count("import socketserver\n")!=1 or "import stat\n" in code:
        raise RuntimeError("Unexpected Collective Memory import layout")
    code=code.replace("import socketserver\n","import socketserver\nimport stat\n")
    code=code.replace(old_paths,patched_paths)
    code=code.replace(old_serve,patched_serve+"    # Refuse to steal a live service's socket.")
    # Keep one atomic write only after all structural checks succeed.
    tmp=path.with_name(path.name+".socket-repair.tmp")
    tmp.write_text(code,encoding="utf-8")
    tmp.chmod(path.stat().st_mode & 0o777)
    tmp.replace(path)
    return True


def _mask_memory(dbpath, target, alias):
    """Mask direct target identity while retaining existing embedding rows."""
    with sqlite3.connect(dbpath) as con:
        con.row_factory=sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        for row in con.execute("SELECT * FROM findings").fetchall():
            updated={k:re.sub(re.escape(target),alias,str(row[k]),flags=re.I)
                     for k in ("title","summary","details")}
            direct=row["va"].lower()==target.lower()
            if direct or any(updated[k]!=row[k] for k in updated):
                con.execute("""UPDATE findings SET va=?,title=?,summary=?,details=?,
                    evidence_path=? WHERE id=?""",
                    (alias if direct else row["va"],updated["title"],updated["summary"],
                     updated["details"],"" if direct else row["evidence_path"],row["id"]))
                con.execute("DELETE FROM findings_fts WHERE rowid=?",(row["id"],))
                con.execute("INSERT INTO findings_fts(rowid,title,summary,details) VALUES(?,?,?,?)",
                            (row["id"],updated["title"],updated["summary"],updated["details"]))
            for e in con.execute("SELECT ordinal,command,result FROM finding_experiments WHERE finding_id=?",
                                 (row["id"],)).fetchall():
                newcmd=re.sub(re.escape(target),alias,e["command"],flags=re.I)
                newresult=re.sub(re.escape(target),alias,e["result"],flags=re.I)
                if newcmd!=e["command"] or newresult!=e["result"]:
                    con.execute("UPDATE finding_experiments SET command=?,result=? WHERE finding_id=? AND ordinal=?",
                                (newcmd,newresult,row["id"],e["ordinal"]))
        # Also remove direct address references in technique evidence and labels.
        for row in con.execute("SELECT * FROM compiler_techniques").fetchall():
            keys=("title","problem","conditions","intervention","observed_effect")
            newer={key:re.sub(re.escape(target),alias,row[key],flags=re.I) for key in keys}
            if any(newer[key]!=row[key] for key in keys):
                con.execute("""UPDATE compiler_techniques SET title=?,problem=?,conditions=?,
                   intervention=?,observed_effect=? WHERE id=?""",
                   (*(newer[key] for key in keys),row["id"]))
                con.execute("DELETE FROM compiler_techniques_fts WHERE rowid=?",(row["id"],))
                con.execute("""INSERT INTO compiler_techniques_fts
                     (rowid,title,category,problem,conditions,intervention,observed_effect)
                     VALUES(?,?,?,?,?,?,?)""",
                     (row["id"],newer["title"],row["category"],newer["problem"],
                      newer["conditions"],newer["intervention"],newer["observed_effect"]))
        for row in con.execute("SELECT technique_id,finding_id,evidence_quote,observation FROM compiler_technique_links").fetchall():
            quote=re.sub(re.escape(target),alias,row["evidence_quote"],flags=re.I)
            note=re.sub(re.escape(target),alias,row["observation"],flags=re.I)
            if quote!=row["evidence_quote"] or note!=row["observation"]:
                con.execute("""UPDATE compiler_technique_links SET evidence_quote=?,observation=?
                   WHERE technique_id=? AND finding_id=?""",
                   (quote,note,row["technique_id"],row["finding_id"]))
        # Freeze the original vectors for the entire pilot. Do not re-embed
        # after masking: it would change the independent variable.
        con.execute("DELETE FROM embedding_jobs")
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='technique_embedding_jobs'").fetchone():
            con.execute("DELETE FROM technique_embedding_jobs")
        con.commit()


def _clean_operational(dbpath, target):
    """Remove all previous attempts and sessions in the COPY only."""
    with sqlite3.connect(dbpath) as con:
        con.execute("PRAGMA foreign_keys=ON")
        # Follow foreign-key order; keep the original queue and budget provenance.
        for table in ("attempt_reviews","run_delays","run_attempt_limits",
                      "run_exclusions","claims","inference_slots","reservations",
                      "assignments","runs","agent_sessions","provider_incidents",
                      "attempts","run_budgets"):
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                           (table,)).fetchone():
                con.execute("DELETE FROM "+table)
        con.commit()
        if con.execute("SELECT COUNT(*) FROM attempts WHERE va=?",(target,)).fetchone()[0]:
            raise RuntimeError("Target history was not cleared in the experiment clone")


def _clone(seed, destination):
    if destination.exists():
        raise FileExistsError(f"Experiment sandbox exists: {destination}")
    shutil.copytree(seed,destination,symlinks=True,ignore=_copy_ignore(seed))
    _relocate_symlinks(destination,seed)
    # Ignore logic removes SQLite files; install immutable prepared snapshots.
    shutil.copy2(seed/OPS,destination/OPS)
    if (seed/KNOW).is_file():
        (destination/KNOW).parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(seed/KNOW,destination/KNOW)
    # The virtualenv is a shared dependency only, never a source workspace.
    if (seed/".venv").is_symlink():
        (destination/".venv").symlink_to(os.readlink(seed/".venv"),target_is_directory=True)
    if (seed/WORK/"memory/.venv").is_symlink():
        (destination/WORK/"memory/.venv").symlink_to(
            os.readlink(seed/WORK/"memory/.venv"),target_is_directory=True)


def prepare(repo, out, va=None, repeats=3, minimum=7, seed=20261118,
            resume_partial=False):
    repo=repo.resolve();out=out.resolve()
    if out==repo or out.is_relative_to(repo) or repo.is_relative_to(out):
        raise ValueError("Experiment directory must be outside the live repo and its ancestors")
    if out.exists() and not resume_partial:
        raise FileExistsError("Output directory exists; refusing to overwrite a previous experiment")
    if not out.exists() and resume_partial:
        raise FileNotFoundError("No partial benchmark directory to resume")
    found=candidates(repo,minimum)
    if va:
        va=canonical(va)
        choice=next((x for x in found if x["va"]==va),None)
        if choice is None:
            raise ValueError("Chosen VA is not a failed, isolated slice with enough attempts")
    elif found:
        choice=found[0]
        va=choice["va"]
    else:
        raise ValueError("No eligible standalone slice. Run candidates and inspect target selection.")
    if not (repo/RUNNER).is_file() or not (repo/MEMORY).is_file():
        raise ValueError("Install the SAD runner and Collective Memory in the source checkout first")
    if not (repo/KNOW).is_file():
        raise ValueError("Collective Memory SQLite missing; consolidation has not been installed")
    source=repo/slice_path(va)
    original=source.read_text(encoding="utf-8",errors="replace")
    others=set(re.findall(r"(?mi)^\s*//\s*@\s*(0x[0-9a-f]{8})",original))
    if others-{va}:
        raise ValueError("Source contains other function anchors; refusing to erase a shared file")
    if resume_partial:
        seed_tree=_validate_partial(repo,out,va,original)
    else:
        out.mkdir(parents=True,exist_ok=False)
    try:
        archive=out/"archive"
        if not resume_partial:
            archive.mkdir()
            snapshot_db(repo/OPS,archive/"spore.sqlite3")
            snapshot_db(repo/KNOW,archive/"knowledge.sqlite3")
            (archive/"original-target.cpp").write_text(original,encoding="utf-8")
            (archive/"original-target.sha256").write_text(
                hashlib.sha256(original.encode()).hexdigest()+"\n")
            # These are outside every agent workspace, immutable experiment provenance.
            with sqlite3.connect(archive/"spore.sqlite3") as con:
                con.row_factory=sqlite3.Row
                history=[dict(x) for x in con.execute(
                    "SELECT * FROM attempts WHERE va=? ORDER BY id",(va,))]
            write_json(archive/"target-attempts.json",history)
            seed_tree=out/"seed"
            print("Copying entire checkout into an isolated seed:",seed_tree,flush=True)
            shutil.copytree(repo,seed_tree,symlinks=True,ignore=_copy_ignore(repo))
        else:
            print("Resuming validated partial seed without recopying:",seed_tree,flush=True)
        remapped=_relocate_symlinks(seed_tree,repo)
        if remapped:
            print(f"Relocated {remapped} in-repository symlink(s) into isolated seed.",flush=True)
        if (repo/".venv").is_dir():
            (seed_tree/".venv").symlink_to(repo/".venv",target_is_directory=True)
        if (repo/WORK/"memory/.venv").is_dir():
            (seed_tree/WORK/"memory/.venv").symlink_to(
                repo/WORK/"memory/.venv",target_is_directory=True)
        shutil.copy2(archive/"spore.sqlite3",seed_tree/OPS)
        shutil.copy2(archive/"knowledge.sqlite3",seed_tree/KNOW)
        # Source reset only in the seed, never in the live checkout.
        blank=(f"// @ {va}\n"
               "// Controlled blank-slate experiment. Rebuild using task.py and check.py.\n")
        (seed_tree/slice_path(va)).write_text(blank,encoding="utf-8")
        _clean_operational(seed_tree/OPS,va)
        alias="0xff"+hashlib.sha256(va.encode()).hexdigest()[:6]
        with sqlite3.connect(seed_tree/KNOW) as con:
            if con.execute("SELECT 1 FROM findings WHERE va=?",(alias,)).fetchone():
                raise RuntimeError("Address anonymization collision")
        _mask_memory(seed_tree/KNOW,va,alias)
        _patch_runner(seed_tree/RUNNER)
        _patch_memory_socket(seed_tree/MEMORY)
        shutil.copy2(RETRIEVAL_SOURCE,seed_tree/RETRIEVAL)
        shutil.copy2(ENTRY_SOURCE,seed_tree/ENTRY)
        rng=random.Random(seed)
        order=[{"arm":arm,"repeat":i+1} for i in range(repeats) for arm in ARMS]
        rng.shuffle(order)
        for item in order:
            item["name"]=f"trial-{len([x for x in order if 'name' in x]):02d}-{rng.getrandbits(32):08x}"
            item["state"]="prepared"
            item["seed"]=rng.randrange(1,2**31)
        write_json(out/"plan.json",{
            "version":1,"created":now(),"source":str(repo),
            "target":va,"alias":alias,"original_failures":choice["failures"],
            "original_cpp_sha256":hashlib.sha256(original.encode()).hexdigest(),
            "repeats":repeats,"random_seed":seed,
            "model":"deepseek/deepseek-v4.1-flash-1017","provider":"sail-research",
            "tooling_sha256":{name.as_posix():_tool_hash(seed_tree/name)
                              for name in (ENTRY,RUNNER,RETRIEVAL,MEMORY)},
            "arms":order,
            "notes":"No paid calls performed; each run creates an isolated tree."})
        print(f"Prepared {len(order)} randomized, isolated test slots for {va}.")
        print("Next: python3 brain_ablation.py run-next --out "+str(out)+" --execute")
        return out
    except BaseException:
        # Fail closed. Retain any partial archive for inspection, no deletion.
        print("Preparation failed. Partial output retained for inspection:",out,file=sys.stderr)
        raise


def _vector_start_failure(logfile, explanation):
    """Expose the actual offline service traceback, without miscounting a trial.

    Startup runs before paid inference. Limit diagnostics to 30 lines/4000
    characters and redact common credential header forms.
    """
    tail="(No memory-service output was recorded.)"
    if logfile.is_file():
        lines=logfile.read_text(encoding="utf-8",errors="replace").splitlines()
        tail="\n".join(lines[-30:])[-4000:] or tail
        tail=re.sub(r"(?i)(bearer\s+)[^\s,]+",r"\1[REDACTED]",tail)
        tail=re.sub(r"(?i)(OPENROUTER_API_KEY\s*[=:]\s*)[^\s]+",
                    r"\1[REDACTED]",tail)
    return RuntimeError(
        explanation+"\nMemory service log: "+str(logfile)+"\nLast output:\n"+tail)


def _start_vectors(trial, python):
    vector_python=trial/WORK/"memory/.venv/bin/python"
    if not vector_python.is_file():
        raise RuntimeError("Dedicated FastEmbed/sqlite-vec venv missing in experiment copy")
    python=vector_python
    script=trial/MEMORY
    if not (trial/KNOW).is_file():
        raise RuntimeError("Memory database is required for this treatment")
    logfile=trial/WORK/"benchmark-memory-service.log"
    logfile.parent.mkdir(parents=True,exist_ok=True)
    with logfile.open("wb") as output:
        process=subprocess.Popen([str(python),str(trial/ENTRY),"memory",
                                  "--repo",str(trial),"serve"],
                                  cwd=trial,stdin=subprocess.DEVNULL,stdout=output,
                                  stderr=subprocess.STDOUT,start_new_session=True,
                                  env={**os.environ,"PYTHONDONTWRITEBYTECODE":"1"})
    # Only the Brain knows whether it selected a short socket for this clone.
    # Read its status endpoint rather than guessing the old memory.sock path.
    for _ in range(60):
        if process.poll() is not None:
            raise _vector_start_failure(
                logfile, f"Vector service exited during startup (exit {process.returncode})")
        try:
            check=subprocess.run([str(python),str(trial/ENTRY),"memory",
                                  "--repo",str(trial),"status"],
                                 cwd=trial,capture_output=True,text=True,timeout=5)
            if check.returncode==0:
                state=json.loads(check.stdout)
                if state.get("service_online") and not state.get("restart_required"):
                    return process
        except (OSError,ValueError,subprocess.TimeoutExpired):
            pass
        time.sleep(1)
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    raise _vector_start_failure(
        logfile, "Vector service did not become ready; no paid agent was started")


def _python(trial):
    preferred=trial/".venv/bin/python"
    return preferred if preferred.is_file() else Path(sys.executable)


def _tool_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_tooling(out, plan):
    """Fail before cloning or spending if the seed lacks its own safe launcher."""
    seed=out/"seed"
    for name in (ENTRY,RUNNER,RETRIEVAL,MEMORY):
        file=seed/name
        if not file.is_file() or file.is_symlink():
            raise RuntimeError(
                f"Benchmark seed missing an isolated tool: {name}. "
                "Run: python3 brain_ablation.py repair-seed --out "+str(out))
    runner=(seed/RUNNER).read_text(encoding="utf-8")
    if ("brain_trial_entry.py query" not in runner or
            ".venv/bin/python work/scratch/brain_trial_query.py --text" in runner):
        raise RuntimeError(
            "The benchmark seed contains the obsolete Brain query entrypoint. "
            "Run: python3 brain_ablation.py repair-seed --out "+str(out))
    if SOCKET_MARKER not in (seed/MEMORY).read_text(encoding="utf-8"):
        raise RuntimeError(
            "The frozen Brain memory module needs the short socket shim. "
            "Run: python3 brain_ablation.py repair-seed --out "+str(out))
    expected=plan.get("tooling_sha256",{})
    for name in (ENTRY,RUNNER,RETRIEVAL,MEMORY):
        key=name.as_posix()
        if key in expected and _tool_hash(seed/name)!=expected[key]:
            raise RuntimeError(f"Benchmark seed tooling changed after preparation: {key}")


def repair_seed(out):
    """Upgrade ONLY benchmark-owned tooling in an unmeasured saved seed.

    A trial previously started from this seed can have a missing entrypoint
    even after updating the orchestrator. Preserve all trial copies/logs and
    the frozen knowledge snapshots. Refuse repair if a real run ever began.
    """
    out=out.resolve()
    planpath=out/"plan.json"
    plan=json.loads(planpath.read_text(encoding="utf-8"))
    seed=out/"seed"
    if not seed.is_dir() or not (seed/OPS).is_file() or not (seed/KNOW).is_file():
        raise RuntimeError("Prepared benchmark seed or SQLite snapshots missing")
    if any(i.get("state") not in {"prepared","error","blocked","interrupted"}
           for i in plan["arms"]):
        raise RuntimeError("Cannot change benchmark tooling after a recorded or running trial")
    expected=f"// @ {plan['target']}\n// Controlled blank-slate experiment."
    target=seed/slice_path(plan["target"])
    if not target.is_file() or not target.read_text(encoding="utf-8").startswith(expected):
        raise RuntimeError("Prepared target source changed; seed repair rejected")
    with sqlite3.connect(f"file:{(seed/OPS).resolve()}?mode=ro",uri=True) as db:
        if db.execute("SELECT COUNT(*) FROM attempts WHERE va=?",(plan["target"],)).fetchone()[0]:
            raise RuntimeError("Target attempts found in seed; refusing repair")
    # Include retired attempts named in invalid_history, not just current slots.
    seen=set()
    for item in plan["arms"]:
        for name in [item["name"],*(x["name"] for x in item.get("invalid_history",[]))]:
            if name in seen:
                raise RuntimeError("Duplicate trial sandbox name in plan")
            seen.add(name)
            trial=out/"trials"/name
            if not trial.exists():
                # Unstarted slots have no trial directory.
                if name==item["name"] and item["state"]!="prepared":
                    raise RuntimeError(f"Invalid trial missing its evidence directory: {name}")
                continue
            if not trial.is_dir() or not (trial/OPS).is_file():
                raise RuntimeError(f"Cannot audit failed trial: {name}")
            with sqlite3.connect(f"file:{(trial/OPS).resolve()}?mode=ro",uri=True) as db:
                attempts=db.execute("SELECT COUNT(*) FROM attempts WHERE va=?",
                                    (plan["target"],)).fetchone()[0]
                charged=db.execute("SELECT COALESCE(SUM(last_usage-baseline_usage),0) "
                                   "FROM run_budgets").fetchone()[0]
            logs=trial/WORK/"logs"
            audit=trial/WORK/"brain-queries.jsonl"
            source=trial/slice_path(plan["target"])
            if (attempts or float(charged or 0)>0.000001 or
                    (logs.is_dir() and any(logs.rglob("*.jsonl"))) or
                    (audit.is_file() and audit.stat().st_size>0) or
                    not source.is_file() or
                    not source.read_text(encoding="utf-8").startswith(expected)):
                raise RuntimeError(
                    f"Trial {name} has possible agent activity. "
                    "Cannot modify shared seed after an experimental run.")
    runner=seed/RUNNER
    query=seed/RETRIEVAL
    launcher=seed/ENTRY
    if any(p.is_symlink() for p in (runner,query,launcher,seed/MEMORY)):
        raise RuntimeError("Benchmark tooling must never be symlinks")
    code=runner.read_text(encoding="utf-8")
    old_cmd=".venv/bin/python work/scratch/brain_trial_query.py --text"
    new_cmd=".venv/bin/python brain_trial_entry.py query --text"
    if old_cmd in code:
        if code.count(old_cmd)!=1 or new_cmd in code:
            raise RuntimeError("Unexpected Brain query prompt, refusing ambiguous repair")
        code=code.replace(old_cmd,new_cmd)
    if ("if os.environ.get('SAD_BENCH_ACTIVE')" not in code or
            code.count(new_cmd)!=1):
        raise RuntimeError("Runner lacks the expected benchmark prompt; refusing repair")
    # Update the socket address selection in this clone only, leaving the
    # independent memory DB, vectors and evidence exactly as archived.
    _patch_memory_socket(seed/MEMORY)
    # Apply only benchmark-owned scripts. SQLite, user source, task/checker code,
    # archived evidence, seeds and existing trial data remain unchanged.
    tmp=runner.with_name(runner.name+".repair.tmp")
    tmp.write_text(code,encoding="utf-8")
    tmp.chmod(runner.stat().st_mode & 0o777)
    tmp.replace(runner)
    for src,dst in ((ENTRY_SOURCE,launcher),(RETRIEVAL_SOURCE,query)):
        if not src.is_file():
            raise RuntimeError(f"Missing benchmark tool in SAD checkout: {src}")
        tmp=dst.with_name(dst.name+".repair.tmp")
        shutil.copy2(src,tmp)
        tmp.replace(dst)
    plan["tooling_sha256"]={name.as_posix():_tool_hash(seed/name)
                             for name in (ENTRY,RUNNER,RETRIEVAL,MEMORY)}
    plan["tooling_repair_utc"]=now()
    write_json(planpath,plan)
    _seed_tooling(out,plan)
    print("Benchmark seed repaired: short Unix socket, launcher and Brain query tools validated.")
    print("Frozen SQLite databases, treatment assignments and failed trial logs unchanged.")
    print("The next action is retry-invalid (no paid inference), then run-next.")


def run_next(out, execute=False, budget=DEFAULT_BUDGET, minutes=20):
    out=out.resolve()
    planpath=out/"plan.json"
    plan=json.loads(planpath.read_text(encoding="utf-8"))
    item=next((x for x in plan["arms"] if x["state"]=="prepared"),None)
    if item is None:
        print("No more prepared trials. Use report.")
        return
    if not execute:
        print("DRY RUN: next arm",item["name"],"target",plan["target"])
        print("This will start a paid OpenRouter decompilation agent only with --execute.")
        print("Local soft budget per arm: $"+format(budget,".2f"))
        return
    if not 0.10<=budget<=25 or not 1<=minutes<=180:
        raise ValueError("Budget must be $0.10-$25 and minutes 1-180")
    _seed_tooling(out,plan)
    env_key=os.environ.get("OPENROUTER_API_KEY","").strip()
    if not env_key:
        keyfile=Path.home()/".config/spore-agentic-decompiler/openrouter.key"
        if keyfile.is_file():
            if keyfile.stat().st_mode & 0o077:
                raise RuntimeError("Configured OpenRouter key has insecure permissions; requires 0600")
            env_key=keyfile.read_text(encoding="utf-8").strip()
    if not env_key:
        raise RuntimeError("OpenRouter key missing (environment or private SAD key file); no paid work started")
    trial=out/"trials"/item["name"]
    _clone(out/"seed",trial)
    if item["arm"]=="baseline":
        (trial/KNOW).unlink(missing_ok=True)
    elif item["arm"]=="raw":
        # No consolidated techniques can be returned in this arm, even if an
        # agent tries the normal memory CLI rather than the benchmark wrapper.
        with sqlite3.connect(trial/KNOW) as con:
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("DELETE FROM compiler_technique_links")
            con.execute("DELETE FROM compiler_techniques_fts")
            con.execute("DELETE FROM compiler_techniques")
            con.commit()
    write_json(trial/CONFIG,{"arm":item["arm"],"repeat":item["repeat"],
                             "target":plan["target"],"alias":plan["alias"],
                             "seed":item["seed"],"source_sha256":plan["original_cpp_sha256"]})
    if (trial/slice_path(plan["target"])).read_text(encoding="utf-8").find(
          "Controlled blank-slate experiment")<0:
        raise RuntimeError("Fresh source state was not preserved")
    with sqlite3.connect(trial/OPS) as con:
        if con.execute("SELECT COUNT(*) FROM attempts WHERE va=?",
                       (plan["target"],)).fetchone()[0]:
            raise RuntimeError("Old target attempts leaked into trial")
    python=_python(trial)
    # Service startup occurs BEFORE charging for the agent. Inability to run
    # actual semantic retrieval invalidates the treatment, not fallback to FTS.
    service=None
    try:
        if item["arm"]!="baseline":
            service=_start_vectors(trial,python)
        item["state"]="running"
        item["started"]=now()
        write_json(planpath,plan)
        cmd=[str(python),str(trial/ENTRY),"runner","run",
             "--only-va",plan["target"],"--passes","1","--max-functions","1",
             "--backend","openrouter","--reasoning","high",
             "--quota-wait","0","--provider-retries","0",
             "--max-inflight","1","--minutes",str(minutes),"--budget",str(budget)]
        env={**os.environ,
             "OPENROUTER_API_KEY":env_key,
             "SAD_BENCH_ACTIVE":"1",
             "SPORE_ADAPTIVE_INFLIGHT":"0",
             "PYTHONDONTWRITEBYTECODE":"1",
             "PWD":str(trial)}
        print("Running",item["name"],"in",trial,flush=True)
        with (trial/"benchmark-run.log").open("w",encoding="utf-8") as log:
            proc=subprocess.run(cmd,cwd=trial,env=env,stdout=log,stderr=subprocess.STDOUT)
        item["exit_code"]=proc.returncode
        with sqlite3.connect(trial/OPS) as db:
            completed=db.execute("SELECT 1 FROM attempts WHERE va=? LIMIT 1",
                                  (plan["target"],)).fetchone() is not None
        item["state"]="completed" if proc.returncode==0 and completed else "error"
        if not completed:
            item["error"]="Runner exited without a recorded decompilation attempt"
        item["finished"]=now()
        write_json(planpath,plan)
        print("Trial",item["name"],"finished with exit code",proc.returncode,
              "and",("an independent result" if completed else "NO_RESULT"),flush=True)
        if item["state"]=="error":
            logfile=trial/"benchmark-run.log"
            print("Trial invalid. Diagnostics:",logfile,flush=True)
            if logfile.exists():
                # Reproduce only a bounded tail, never print credentials.
                lines=logfile.read_text(encoding="utf-8",errors="replace").splitlines()
                print("Last runner output:\n"+ "\n".join(lines[-35:]),flush=True)
    except BaseException as exc:
        item["state"]="blocked" if item["state"]=="prepared" else "interrupted"
        item["error"]=type(exc).__name__+": "+str(exc)[:350]
        item["finished"]=now()
        write_json(planpath,plan)
        raise
    finally:
        if service and service.poll() is None:
            service.terminate()
            try:
                service.wait(timeout=8)
            except subprocess.TimeoutExpired:
                service.kill()
                service.wait()


def retry_invalid(out, name=None):
    """Requeue a demonstrably unmeasured slot, preserving all previous diagnostics.

    No API calls. Never overwrite, delete or reuse the failed sandbox. Refuse
    uncertain attempts: a result, an agent JSONL trace, Brain activity, or
    charged usage is enough to prohibit an automatic replacement.
    """
    import uuid
    out=out.resolve()
    planpath=out/"plan.json"
    plan=json.loads(planpath.read_text(encoding="utf-8"))
    found=[i for i in plan["arms"] if i["state"] in {"error","blocked","interrupted"}]
    if name:
        found=[i for i in found if i["name"]==name]
    if len(found)!=1:
        raise RuntimeError("Select one invalid trial with --name, or ensure exactly one exists")
    item=found[0]
    trial=out/"trials"/item["name"]
    if not trial.is_dir():
        raise RuntimeError("Trial directory missing; cannot audit before retry")
    with sqlite3.connect(f"file:{(trial/OPS).resolve()}?mode=ro",uri=True) as con:
        counted=con.execute("SELECT COUNT(*) FROM attempts WHERE va=?",
                            (plan["target"],)).fetchone()[0]
        usage=con.execute("SELECT COALESCE(SUM(last_usage-baseline_usage),0) "
                          "FROM run_budgets").fetchone()[0]
    if counted:
        raise RuntimeError("Trial has recorded decompilation attempts; refusing a replacement")
    if float(usage or 0)>0.000001:
        raise RuntimeError("Trial recorded paid inference; manual review required")
    logs=trial/WORK/"logs"
    if logs.exists() and any(logs.rglob("*.jsonl")):
        raise RuntimeError("Agent JSONL evidence exists; manual review required before retry")
    audit=trial/WORK/"brain-queries.jsonl"
    if audit.exists() and audit.stat().st_size:
        raise RuntimeError("Brain was queried; cannot label this a pre-agent failure")
    original=trial/slice_path(plan["target"])
    expected=f"// @ {plan['target']}\n// Controlled blank-slate experiment."
    if not original.read_text(encoding="utf-8").startswith(expected):
        raise RuntimeError("Target source was changed; cannot safely classify as pre-agent failure")
    old_name=item["name"]
    replacement="trial-retry-"+uuid.uuid4().hex[:16]
    if (out/"trials"/replacement).exists():
        raise RuntimeError("Replacement sandbox name collides")
    item.setdefault("invalid_history",[]).append({
        "name":old_name,"state":item["state"],"exit_code":item.get("exit_code"),
        "reason":item.get("error",""),"retired_utc":now()
    })
    item["name"]=replacement
    item["state"]="prepared"
    for field in ("error","started","finished","exit_code"):
        item.pop(field,None)
    write_json(planpath,plan)
    print(f"Requeued {item['arm']} repeat {item['repeat']} as {replacement}.")
    print(f"Original invalid trial preserved, including benchmark-run.log: {trial}")
    print("No inference started. Inspect the old log before using run-next --execute.")


def _checker_calls(folder):
    root=folder/WORK/"logs"
    calls=0;input_tokens=output_tokens=0
    if not root.is_dir():
        return 0,0,0
    for path in root.rglob("*.jsonl"):
        with path.open(encoding="utf-8",errors="replace") as handle:
            for line in handle:
                try:
                    event=json.loads(line)
                except (ValueError,json.JSONDecodeError):
                    continue
                part=event.get("part") or {}
                state=part.get("state") or {}
                invocation=state.get("input") or {}
                command=str(invocation.get("command") or invocation.get("cmd") or "")
                if event.get("type")=="tool_use" and "tools/contrib/check.py" in command:
                    calls+=1
                if event.get("type")=="step_finish":
                    tokens=part.get("tokens") or {}
                    input_tokens+=int(tokens.get("input") or 0)
                    output_tokens+=int(tokens.get("output") or 0)
    return calls,input_tokens,output_tokens


def _knowledge_exposure(arm, audit_path):
    """Measure real tool responses, not only assignment to a treatment arm."""
    counts={"calls":0,"findings":0,"techniques":0,"characters":0}
    if audit_path.is_file():
        for line in audit_path.read_text(encoding="utf-8",errors="replace").splitlines():
            try:
                record=json.loads(line)
            except (ValueError,TypeError):
                continue
            if not isinstance(record,dict):
                continue
            counts["calls"]+=1
            for field,source in (("findings","findings"),("techniques","techniques"),
                                 ("characters","characters")):
                try:
                    counts[field]+=max(0,int(record.get(source,0)))
                except (ValueError,TypeError):
                    continue
    if arm=="baseline":
        # Baseline is intentionally not given knowledge, even if it tries.
        counts["exposure"]="none"
        counts["adherent"]=True
    elif counts["calls"]==0:
        counts["exposure"]="not_queried"
        counts["adherent"]=False
    elif arm=="raw":
        counts["exposure"]="raw" if counts["findings"] else "queried_empty"
        counts["adherent"]=counts["findings"]>0
    elif arm=="consolidated":
        counts["exposure"]="consolidated" if counts["techniques"] else "no_techniques"
        counts["adherent"]=counts["techniques"]>0
    else:
        counts["exposure"]="placebo" if counts["characters"] else "queried_empty"
        counts["adherent"]=counts["characters"]>0
    return counts


def report(out):
    out=out.resolve()
    plan=json.loads((out/"plan.json").read_text(encoding="utf-8"))
    rows=[]
    for item in plan["arms"]:
        trial=out/"trials"/item["name"]
        data={"name":item["name"],"repeat":item["repeat"],
              "arm":item["arm"],"state":item["state"],
              "match":None,"checker_calls":None,"input_tokens":None,
              "output_tokens":None,"brain_queries":None,"cost_usd":None,
              "result_note":None, "exposure":None, "treatment_adherent":None}
        if (trial/OPS).exists():
            with sqlite3.connect(trial/OPS) as con:
                result=con.execute("SELECT status,note,utc FROM attempts WHERE va=? ORDER BY id DESC LIMIT 1",
                                   (plan["target"],)).fetchone()
                data["status"]=result[0] if result else "NO_RESULT"
                # SKIP is an agent verdict, not a byte mismatch. Keep it in the
                # intention-to-treat match denominator, but distinguish it
                # from an independently checked FAIL.
                data["match"]=(result[0]=="MATCH") if result else None
                data["result_note"]=result[1] if result else None
                diff=re.search(r"\bdiffs?\s*[=:]\s*(\d+)",result[1],re.I) if result and result[1] else None
                data["diff_bytes"]=int(diff.group(1)) if diff else None
                budget=con.execute(
                    "SELECT COALESCE(SUM(last_usage-baseline_usage),0) FROM run_budgets").fetchone()
                data["cost_usd"]=round(max(0.0,float(budget[0] or 0)),6)
            c,t1,t2=_checker_calls(trial)
            data.update(checker_calls=c,input_tokens=t1,output_tokens=t2)
            audit=trial/WORK/"brain-queries.jsonl"
            exposure=_knowledge_exposure(item["arm"],audit)
            data["brain_queries"]=exposure["calls"]
            data["findings_returned"]=exposure["findings"]
            data["techniques_returned"]=exposure["techniques"]
            data["exposure"]=exposure["exposure"]
            data["treatment_adherent"]=exposure["adherent"]
        rows.append(data)
    write_json(out/"report.json",{"target":plan["target"],"generated":now(),"trials":rows})
    print("ARM             REP  STATE       RESULT     AGENT CHECKS  BRAIN  INPUT TOK.   OUTPUT TOK.  $ (shared key)")
    for x in rows:
        print(f"{x['arm']:<16}{x['repeat']:>3}  {x['state']:<12}"
              f"{x.get('status','-'):<10} {str(x['checker_calls']):>12}  "
              f"{str(x['brain_queries']):>5}  {str(x['input_tokens']):>10}  "
              f"{str(x['output_tokens']):>11}  {str(x['cost_usd']):>8}")
        if x.get("status") in ("SKIP","FAIL","DEFER") and x.get("result_note"):
            print("     Note:",str(x["result_note"]).replace("\n"," ")[:500])
        if x.get("brain_queries") is not None and x["state"]=="completed":
            print("     Exposure:",x["exposure"],"| treatment used:",
                  "yes" if x["treatment_adherent"] else "NO",
                  "| findings:",x.get("findings_returned",0),
                  "| techniques:",x.get("techniques_returned",0))
    print("\nSummary by arm (intention to treat, includes SKIP as no match):")
    for arm in ARMS:
        group=[x for x in rows if x["arm"]==arm and x["match"] is not None]
        if group:
            matches=sum(int(x["match"]) for x in group)
            tested=sum(x.get("status")!="SKIP" for x in group)
            adhered=sum(bool(x.get("treatment_adherent")) for x in group)
            avg_checks=sum(x["checker_calls"] or 0 for x in group)/len(group)
            avg_tokens=sum((x["input_tokens"] or 0)+(x["output_tokens"] or 0)
                           for x in group)/len(group)
            print(f"  {arm:<13}: {matches}/{len(group)} MATCH,"
                  f" {tested}/{len(group)} non-SKIP outcomes,"
                  f" {adhered}/{len(group)} treatment adherence,"
                  f" {avg_checks:.1f} agent checks/trial, {avg_tokens:.0f} tokens/trial")
    print("\nSKIP is the agent's decision, not an independently verified mismatch.")
    print("AGENT CHECKS count shell check.py calls observed in the agent's JSONL;")
    print("the runner's independent verification is separate and is recorded in result notes.")
    print("Assignment without Brain retrieval is not proof that knowledge was evaluated.")
    print("Provider-key cost may include other paid jobs; pause them for reliable comparison.")

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="command",required=True)
    c=sub.add_parser("candidates",help="Read-only: rank failed functions with dedicated slices")
    c.add_argument("--repo",type=Path,default=Path.home()/"spore-decomp")
    c.add_argument("--min-failures",type=int,default=7)
    p=sub.add_parser("prepare",help="Make immutable backups and independent clean-slate trial seed")
    p.add_argument("--repo",type=Path,default=Path.home()/"spore-decomp")
    p.add_argument("--out",type=Path,default=Path.home()/"sad-brain-benchmark")
    p.add_argument("--va")
    p.add_argument("--repeats",type=int,default=3)
    p.add_argument("--min-failures",type=int,default=7)
    p.add_argument("--seed",type=int,default=20261118)
    p.add_argument("--resume-partial",action="store_true",
                   help="Validate and resume a pre-database partial preparation without recopying")
    r=sub.add_parser("run-next",help="Run one randomized arm, paid only with --execute")
    r.add_argument("--out",type=Path,default=Path.home()/"sad-brain-benchmark")
    r.add_argument("--execute",action="store_true")
    r.add_argument("--budget",type=float,default=DEFAULT_BUDGET)
    r.add_argument("--minutes",type=int,default=20)
    o=sub.add_parser("report",help="Read-only comparison from independently checked trial logs")
    o.add_argument("--out",type=Path,default=Path.home()/"sad-brain-benchmark")
    seed_fix=sub.add_parser("repair-seed",
        help="Fix missing benchmark-only tools in a frozen seed with no measured trials")
    seed_fix.add_argument("--out",type=Path,default=Path.home()/"sad-brain-benchmark")
    retry=sub.add_parser("retry-invalid",help="Requeue an auditable zero-evidence infrastructure failure")
    retry.add_argument("--out",type=Path,default=Path.home()/"sad-brain-benchmark")
    retry.add_argument("--name",help="The prior invalid sandbox name, if more than one")
    a=parser.parse_args(argv)
    if a.command=="candidates":
        for x in candidates(a.repo.resolve(),a.min_failures)[:25]:
            print(f"{x['va']}  {x['failures']:>2} failed passes  {x['size']:>4} bytes  {x['source']}")
    elif a.command=="prepare":
        if not 1<=a.repeats<=10 or a.min_failures<1:
            parser.error("repeats must be 1-10 and min-failures >= 1")
        prepare(a.repo,a.out,a.va,a.repeats,a.min_failures,a.seed,
                resume_partial=a.resume_partial)
    elif a.command=="run-next":
        run_next(a.out,a.execute,a.budget,a.minutes)
    elif a.command=="report":
        report(a.out)
    elif a.command=="retry-invalid":
        retry_invalid(a.out,a.name)
    elif a.command=="repair-seed":
        repair_seed(a.out)


if __name__=="__main__":
    main()
