"""Offline safety and fidelity tests for the four-arm Brain ablation harness."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import socket
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("brain_ablation_test",ROOT/"brain_ablation.py")
bench=importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)

VA="0x00a1b2c3"
OTHER="0x00d00030"


class BrainAblationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.source=self.base/"upstream"
        self.source.mkdir()
        self.destination=self.base/"isolated-benchmark"
        (self.source/bench.WORK/"memory").mkdir(parents=True)
        (self.source/bench.RUNNER).parent.mkdir(parents=True,exist_ok=True)
        (self.source/bench.PROMPT).write_text(
            (ROOT/"engine/spore_first_pass_prompt.txt").read_text(encoding="utf-8"),
            encoding="utf-8")
        (self.source/bench.RUNNER).write_text("""import os
def run_one():
    prompt = "source"
    if collective_memory.paths(ROOT)[2].is_file():
        prompt += "brain"
    if os.environ.get('SPORE_SQLITE_RUN_ID'):
        prompt += f\"\"\"PARALLEL\"\"\"
""",encoding="utf-8")
        # Simulate an older installed Brain version with a long Unix-socket path.
        self._make_legacy_memory(self.source/bench.MEMORY)
        source_evidence=ROOT/"engine/spore_provider_fix/work/scratch/spore_memory_evidence.py"
        (self.source/bench.MEMORY).with_name("spore_memory_evidence.py").write_bytes(
            source_evidence.read_bytes())
        cpp=self.source/bench.slice_path(VA)
        cpp.parent.mkdir(parents=True)
        cpp.write_text(f"// @ {VA}\nint repaired() {{ return 123; }}\n",encoding="utf-8")
        self.original=cpp.read_text()
        with sqlite3.connect(self.source/bench.OPS) as db:
            db.executescript("""CREATE TABLE queue (va TEXT PRIMARY KEY,size INTEGER);
                CREATE TABLE attempts
                (id INTEGER PRIMARY KEY AUTOINCREMENT,va TEXT,status TEXT,note TEXT,utc TEXT);
                CREATE TABLE attempt_reviews(attempt_id INTEGER PRIMARY KEY);
                CREATE TABLE agent_sessions(va TEXT,session_id TEXT);
                CREATE TABLE run_budgets(
                    run_id TEXT,baseline_usage REAL,last_usage REAL);""")
            db.execute("INSERT INTO queue VALUES(?,?)",(VA,120))
            db.execute("INSERT INTO queue VALUES(?,?)",(OTHER,80))
            for i in range(7):
                db.execute("INSERT INTO attempts(va,status,note,utc) VALUES(?,?,?,?)",
                           (VA,"FAIL",f"Failure {i}",bench.now()))
            db.execute("INSERT INTO agent_sessions VALUES(?,?)",(VA,"ses_secret_previous"))
        with sqlite3.connect(self.source/bench.KNOW) as db:
            db.executescript("""CREATE TABLE findings (
                    id INTEGER PRIMARY KEY,va TEXT,pass_number INTEGER,
                    status TEXT,verified_match INTEGER,title TEXT,summary TEXT,
                    details TEXT,evidence_path TEXT,source_hash TEXT);
                CREATE VIRTUAL TABLE findings_fts USING fts5(title,summary,details);
                CREATE TABLE finding_experiments (
                    finding_id INTEGER,ordinal INTEGER,command TEXT,result TEXT);
                CREATE TABLE compiler_techniques(
                    id INTEGER PRIMARY KEY,title TEXT,category TEXT,problem TEXT,
                    conditions TEXT,intervention TEXT,observed_effect TEXT);
                CREATE VIRTUAL TABLE compiler_techniques_fts USING fts5(
                    title,category,problem,conditions,intervention,observed_effect);
                CREATE TABLE compiler_technique_links(
                    technique_id INTEGER,finding_id INTEGER,
                    evidence_quote TEXT,observation TEXT);
                CREATE TABLE embedding_jobs(finding_id INTEGER);
                CREATE TABLE technique_embedding_jobs(technique_id INTEGER);
                CREATE TABLE finding_vectors(rowid INTEGER PRIMARY KEY,embedding TEXT);""")
            db.execute("INSERT INTO findings VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (1,VA,1,"FAIL",0,f"{VA} failed",
                        f"ABI mismatch in {VA}","tried EBP frame",f"investigations/{VA}",
                        "unchanged-digest"))
            db.execute("INSERT INTO findings_fts VALUES(?,?,?)",
                       (f"{VA} failed",f"ABI mismatch in {VA}","tried EBP frame"))
            db.execute("INSERT INTO finding_experiments VALUES(?,?,?,?)",
                       (1,1,f"check.py {VA}",f"{VA} FAIL"))
            db.execute("INSERT INTO finding_vectors VALUES(?,?)",
                       (1,"precomputed-384d-embedding"))
            db.execute("INSERT INTO embedding_jobs VALUES(?)",(1,))
            db.execute("INSERT INTO compiler_techniques VALUES(?,?,?,?,?,?,?)",
                       (10,f"Calling convention in {VA}","calling_convention",
                        "stack problem","MSVC x86","frame pointer","frame mismatch"))
            db.execute("INSERT INTO compiler_techniques_fts VALUES(?,?,?,?,?,?)",
                       (f"Calling convention in {VA}","calling_convention",
                        "stack problem","MSVC x86","frame pointer","frame mismatch"))
            db.execute("INSERT INTO compiler_technique_links VALUES(?,?,?,?)",
                       (10,1,f"Observed at {VA}","stack-frame mismatch"))

    @staticmethod
    def _make_legacy_memory(destination):
        template=bench.MEMORY_TEMPLATE.read_text(encoding="utf-8")
        start=template.index("def paths(repo):")
        end=template.index("def utc():",start)
        paths_old='''def paths(repo):
    root = Path(repo).expanduser().resolve()
    folder = root / "work/scratch/codex-pass1/memory"
    return root, folder, folder / "knowledge.sqlite3", folder / "memory.sock"


'''
        template=template[:start]+paths_old+template[end:]
        start=template.index("def serve(repo):")
        end=template.index("    # Refuse to steal a live service's socket.",start)
        old_serve='''def serve(repo):
    _, folder, _, sock = paths(repo)
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
'''
        template=template[:start]+old_serve+template[end:]
        template=template.replace("import stat\n","")
        assert bench.SOCKET_MARKER not in template
        destination.write_text(template,encoding="utf-8")

    def test_short_socket_is_per_clone_and_bindable_under_linux(self):
        import importlib.util
        module_path=self.destination/"legacy-spore-memory.py"
        module_path.parent.mkdir(parents=True)
        # The module reads an explicit sibling, so preserve its real source filename.
        source=ROOT/"engine/spore_provider_fix/work/scratch/spore_collective_memory.py"
        evidence=source.with_name("spore_memory_evidence.py")
        module_path=self.destination/"spore_collective_memory.py"
        module_path.write_bytes(source.read_bytes())
        (self.destination/"spore_memory_evidence.py").write_bytes(evidence.read_bytes())
        spec=importlib.util.spec_from_file_location("test_spore_brain_socket",module_path)
        memory=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(memory)
        root=self.destination/("depth"+"x"*120)
        other=self.destination/("depth"+"y"*120)
        _,folder,db,sock=memory.paths(root)
        _,_,_,sock2=memory.paths(other)
        self.assertGreater(len(str(folder/"memory.sock").encode()),107)
        self.assertLess(len(str(sock).encode()),104)
        self.assertNotEqual(sock,sock2)
        self.assertEqual(db,folder/"knowledge.sqlite3")
        self.assertEqual(sock.parent,Path("/tmp")/f"sad-memory-{__import__('os').getuid()}")
        # Actually bind an AF_UNIX socket at the short path, no FastEmbed needed.
        sock.parent.mkdir(mode=0o700,exist_ok=True)
        if sock.parent.stat().st_mode & 0o077:
            self.skipTest("Existing short socket folder permissions are too broad")
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as instance:
            try:
                instance.bind(str(sock))
                self.assertTrue(sock.exists())
            finally:
                sock.unlink(missing_ok=True)

    def test_preflight_rejects_old_seed_without_starting_or_copying(self):
        bench.prepare(self.source,self.destination,repeats=1)
        seed=self.destination/"seed"
        self._make_legacy_memory(seed/bench.MEMORY)
        with mock.patch.object(bench,"_clone",
                               side_effect=AssertionError("old seed was cloned")):
            with self.assertRaisesRegex(RuntimeError,"repair-seed"):
                bench.run_next(self.destination,execute=True,budget=.5)
        bench.repair_seed(self.destination)
        bench._seed_tooling(self.destination,
            json.loads((self.destination/"plan.json").read_text()))
        self.assertIn(bench.SOCKET_MARKER,(seed/bench.MEMORY).read_text())

    def test_repair_old_seed_without_recopying_sqlite_or_trials(self):
        bench.prepare(self.source,self.destination,repeats=1)
        seed=self.destination/"seed"
        planpath=self.destination/"plan.json"
        plan=json.loads(planpath.read_text())
        item=plan["arms"][0]
        active=item["name"]
        archived="trial-original-failed"
        # Imitate the November 18 historical seed, created before the safe
        # root launcher existed, then retried without paid model activity.
        (seed/bench.ENTRY).unlink()
        self._make_legacy_memory(seed/bench.MEMORY)
        runner=seed/bench.RUNNER
        runner.write_text(runner.read_text().replace(
            ".venv/bin/python brain_trial_entry.py query --text",
            ".venv/bin/python work/scratch/brain_trial_query.py --text"))
        bench._clone(seed,self.destination/"trials"/archived)
        bench._clone(seed,self.destination/"trials"/active)
        old_log=self.destination/"trials"/archived/"benchmark-run.log"
        old_log.write_text("ValueError: invalid literal for int() with base 16: 'run'\\n")
        new_log=self.destination/"trials"/active/bench.WORK/"benchmark-memory-service.log"
        new_log.write_text("can't open file brain_trial_entry.py\\n")
        item["state"]="blocked"
        item["invalid_history"]=[{"name":archived,"state":"error"}]
        bench.write_json(planpath,plan)
        original_cpp=(self.source/bench.slice_path(VA)).read_bytes()
        memory_before=bench._tool_hash(seed/bench.MEMORY)
        database_before={str(p):bench._tool_hash(p) for p in [
            seed/bench.OPS,seed/bench.KNOW,
            self.destination/"archive/spore.sqlite3",
            self.destination/"archive/knowledge.sqlite3"]}
        with mock.patch.object(bench.subprocess,"run",
                               side_effect=AssertionError("Repair invoked a paid command")):
            with mock.patch.object(bench.shutil,"copytree",
                                   side_effect=AssertionError("Repair recopied repo")):
                bench.repair_seed(self.destination)
        self.assertTrue((seed/bench.ENTRY).is_file())
        self.assertIn("brain_trial_entry.py query",(seed/bench.RUNNER).read_text())
        self.assertIn(bench.SOCKET_MARKER,(seed/bench.MEMORY).read_text())
        self.assertNotIn("work/scratch/brain_trial_query.py --text",
                         (seed/bench.RUNNER).read_text())
        self.assertEqual(database_before,{str(p):bench._tool_hash(p) for p in [
            seed/bench.OPS,seed/bench.KNOW,
            self.destination/"archive/spore.sqlite3",
            self.destination/"archive/knowledge.sqlite3"]})
        self.assertNotEqual(bench._tool_hash(seed/bench.MEMORY),memory_before)
        self.assertEqual((self.source/bench.slice_path(VA)).read_bytes(),original_cpp)
        self.assertTrue(old_log.exists())
        self.assertTrue(new_log.exists())
        self.assertEqual(json.loads(planpath.read_text())["arms"][0]["name"],active)
        # Idempotence without any trial data loss.
        bench.repair_seed(self.destination)
        bench.retry_invalid(self.destination)
        self.assertTrue(old_log.exists())
        self.assertTrue(new_log.exists())
        self.assertEqual(json.loads(planpath.read_text())["arms"][0]["state"],"prepared")

    def test_v2_claim_skip_is_flagged_protocol_invalid_even_when_runner_exits_zero(self):
        bench.prepare(self.source,self.destination,repeats=1)
        def model_ignoring_instructions(cmd,**kwargs):
            with sqlite3.connect(kwargs["cwd"]/bench.OPS) as db:
                db.execute("INSERT INTO attempts(va,status,note,utc) VALUES(?,?,?,?)",
                           (VA,"SKIP","Pass 1: claimed | checker: no source",bench.now()))
            return mock.Mock(returncode=0)
        with mock.patch.dict("os.environ",{"OPENROUTER_API_KEY":"mock-not-a-real-key"}):
            with mock.patch.object(bench,"_start_vectors",return_value=None):
                with mock.patch.object(bench.subprocess,"run",side_effect=model_ignoring_instructions):
                    bench.run_next(self.destination,execute=True,budget=.5)
        plan=json.loads((self.destination/"plan.json").read_text())
        item=plan["arms"][0]
        self.assertEqual(item["state"],"protocol_invalid")
        bench.report(self.destination)
        row=next(x for x in json.loads((self.destination/"report.json").read_text())["trials"]
                 if x["name"]==item["name"])
        self.assertEqual(row["status"],"SKIP")
        self.assertFalse(row["protocol_valid"])
        self.assertIsNone(row["match"])

    def test_existing_skipped_protocol_is_never_silently_reclassified(self):
        bench.prepare(self.source,self.destination,repeats=1)
        planpath=self.destination/"plan.json"
        plan=json.loads(planpath.read_text(encoding="utf-8"))
        raw=next(i for i in plan["arms"] if i["arm"]=="raw")
        trial=self.destination/"trials"/raw["name"]
        bench._clone(self.destination/"seed",trial)
        with sqlite3.connect(trial/bench.OPS) as db:
            db.execute("INSERT INTO attempts(va,status,note,utc) VALUES(?,?,?,?)",
                       (VA,"SKIP","Pass 1: claimed | checker: no compiled function",bench.now()))
        raw["state"]="completed"
        plan["protocol_version"]=1
        bench.write_json(planpath,plan)
        with self.assertRaisesRegex(RuntimeError,"Cannot change benchmark tooling"):
            bench.repair_seed(self.destination)
        with mock.patch.object(bench,"_clone",
                               side_effect=AssertionError("obsolete trial cloned")):
            with self.assertRaisesRegex(RuntimeError,"obsolete claim/skip protocol"):
                with mock.patch.dict("os.environ",{"OPENROUTER_API_KEY":"mock-not-a-real-key"}):
                    bench.run_next(self.destination,execute=True,budget=.50)
        self.assertEqual(json.loads(planpath.read_text())["protocol_version"],1)
        bench.report(self.destination)
        row=next(x for x in json.loads((self.destination/"report.json").read_text())["trials"]
                 if x["name"]==raw["name"])
        self.assertFalse(row["protocol_valid"])
        self.assertIsNone(row["match"])
        with sqlite3.connect(trial/bench.OPS) as db:
            self.assertEqual(db.execute("SELECT status FROM attempts WHERE va=?",
                                        (VA,)).fetchone()[0],"SKIP")

    def test_repair_seed_rejects_any_started_agent_or_completed_trial(self):
        bench.prepare(self.source,self.destination,repeats=1)
        seed=self.destination/"seed"
        (seed/bench.ENTRY).unlink()
        planpath=self.destination/"plan.json"
        plan=json.loads(planpath.read_text())
        item=plan["arms"][0]
        trial=self.destination/"trials"/item["name"]
        bench._clone(seed,trial)
        evidence=trial/bench.WORK/"logs/model.jsonl"
        evidence.parent.mkdir(parents=True,exist_ok=True)
        evidence.write_text('{"type":"tool_use"}\\n')
        item["state"]="error"
        bench.write_json(planpath,plan)
        with self.assertRaisesRegex(RuntimeError,"possible agent activity"):
            bench.repair_seed(self.destination)
        self.assertFalse((seed/bench.ENTRY).exists())
        self.assertTrue(evidence.exists())

    def test_memory_startup_diagnostics_show_bounded_traceback_and_redact_keys(self):
        logfile=self.base/"memory-service.log"
        logfile.write_text(
            "Loading copied embeddings\n"
            "OPENROUTER_API_KEY=verysecret\n"
            "Authorization: Bearer secondsecret\n"
            "RuntimeError: model cache not found\n",encoding="utf-8")
        message=str(bench._vector_start_failure(
            logfile,"Vector service exited during startup (exit 1)"))
        self.assertIn("model cache not found",message)
        self.assertIn(str(logfile),message)
        self.assertIn("exit 1",message)
        self.assertNotIn("verysecret",message)
        self.assertNotIn("secondsecret",message)

    def test_safe_entrypoint_does_not_shadow_python_dis_module(self):
        workspace=self.base/"collision"
        scratch=workspace/"work/scratch"
        scratch.mkdir(parents=True)
        (workspace/"brain_trial_entry.py").write_text(
            (ROOT/"brain_trial_entry.py").read_text(encoding="utf-8"),
            encoding="utf-8")
        (scratch/"dis.py").write_text(
            "raise RuntimeError('Spore dis.py shadowed Python stdlib')\n")
        (scratch/"spore_test_module.py").write_text("VALUE=123\n")
        script=(
            "import argparse\n"
            "import inspect\n"
            "import dis\n"
            "import spore_test_module\n"
            "assert spore_test_module.VALUE==123\n"
            "assert 'work/scratch/dis.py' not in str(dis.__file__)\n"
            "parser=argparse.ArgumentParser()\n"
            "parser.add_argument('--mode')\n"
            "assert parser.parse_args().mode=='smoke'\n"
            "print('safe-stdlib-and-local-helper')\n"
        )
        for alias, filename in (
            ("runner","spore_first_pass_sqlite.py"),
            ("query","brain_trial_query.py"),
            ("memory","spore_collective_memory.py")):
            (scratch/filename).write_text(script,encoding="utf-8")
            process=subprocess.run(
                [sys.executable,str(workspace/"brain_trial_entry.py"),
                 alias,"--mode","smoke"],cwd=workspace,capture_output=True,text=True)
            self.assertEqual(process.returncode,0,
                             msg=f"{alias}: {process.stdout} {process.stderr}")
            self.assertIn("safe-stdlib-and-local-helper",process.stdout)

    def test_benchmark_query_tool_compiles_and_rejects_direct_address_search(self):
        import ast
        from types import ModuleType
        source=(ROOT/"brain_ablation_query.py").read_text(encoding="utf-8")
        ast.parse(source)
        with mock.patch.dict(sys.modules,{"spore_collective_memory":ModuleType(
                "spore_collective_memory")}):
            queryspec=importlib.util.spec_from_file_location(
                "test_benchmark_query_module",ROOT/"brain_ablation_query.py")
            query=importlib.util.module_from_spec(queryspec)
            queryspec.loader.exec_module(query)
        cfg={"arm":"consolidated","target":VA,"alias":"0xffeeabcd","seed":2}
        output,nfind,ntech=query.lookup(self.source,cfg,
                                       f"Please investigate {VA}")
        self.assertIn("Direct target-address lookup",output)
        self.assertEqual((nfind,ntech),(0,0))

    def test_candidates_use_only_genuine_failed_standalone_slices(self):
        rows=bench.candidates(self.source,7)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["va"],VA)
        self.assertEqual(rows[0]["failures"],7)
        self.assertEqual(bench.candidates(self.source,8),[])

    def test_canonical_benchmark_prompt_ignores_custom_live_prompt_wording(self):
        local=self.source/bench.PROMPT
        local.write_text(
            "Local Spore prompt version 2000. No claim skip wording at all.\n"
            "Only implement byte-exact x86 code for {VA}.\n",encoding="utf-8")
        original=local.read_bytes()
        bench.prepare(self.source,self.destination,repeats=1)
        seed_prompt=(self.destination/"seed"/bench.PROMPT).read_text(encoding="utf-8")
        canonical=bench.PROMPT_SOURCE.read_text(encoding="utf-8")
        self.assertEqual(seed_prompt,canonical)
        self.assertIn("CONTINUE the investigation",seed_prompt)
        self.assertIn(bench.PROTOCOL_MARKER,seed_prompt)
        self.assertEqual(local.read_bytes(),original)
        trial=self.destination/"trials/test-identical-prompt"
        bench._clone(self.destination/"seed",trial)
        self.assertEqual((trial/bench.PROMPT).read_text(),canonical)

    def test_resume_after_prompt_patch_crash_keeps_sqlite_and_full_copy(self):
        # Exact phase seen in the November 18 real environment: cloned,
        # history cleared, knowledge masked, runner patched, prompt failed.
        self.source.joinpath(bench.PROMPT).write_text(
            "Unexpected local prompt wording with no literal claim rule\n")
        with mock.patch.object(bench,"_patch_prompt",
                               side_effect=RuntimeError("old claim-rule mismatch")):
            with self.assertRaisesRegex(RuntimeError,"old claim-rule mismatch"):
                bench.prepare(self.source,self.destination,repeats=1)
        self.assertFalse((self.destination/"plan.json").exists())
        seed=self.destination/"seed"
        self.assertTrue((seed/bench.OPS).is_file())
        self.assertTrue((seed/bench.KNOW).is_file())
        self.assertIn("SAD_BENCH_ACTIVE",(seed/bench.RUNNER).read_text())
        expected="// Controlled blank-slate experiment."
        self.assertIn(expected,(seed/bench.slice_path(VA)).read_text())
        saved={str(p):bench._tool_hash(p) for p in (
            seed/bench.OPS, seed/bench.KNOW,
            self.destination/"archive/spore.sqlite3",
            self.destination/"archive/knowledge.sqlite3")}
        with mock.patch.object(bench.shutil,"copytree",
                               side_effect=AssertionError("repository recopied during resume")):
            with mock.patch.object(bench,"snapshot_db",
                                   side_effect=AssertionError("SQLite snapshot repeated")):
                bench.prepare(self.source,self.destination,repeats=1,resume_partial=True)
        self.assertEqual(saved,{str(p):bench._tool_hash(p) for p in (
            seed/bench.OPS, seed/bench.KNOW,
            self.destination/"archive/spore.sqlite3",
            self.destination/"archive/knowledge.sqlite3")})
        plan=json.loads((self.destination/"plan.json").read_text())
        self.assertEqual(plan["protocol_version"],bench.PROTOCOL_VERSION)
        self.assertEqual(len(plan["arms"]),4)
        self.assertTrue(all(i["state"]=="prepared" for i in plan["arms"]))
        self.assertEqual((seed/bench.PROMPT).read_text(),bench.PROMPT_SOURCE.read_text())
        self.assertEqual((self.source/bench.slice_path(VA)).read_text(),self.original)

    def test_partial_resume_after_stage_b_rejects_modified_target(self):
        with mock.patch.object(bench,"_patch_prompt",
                               side_effect=RuntimeError("failed to install prompt")):
            with self.assertRaises(RuntimeError):
                bench.prepare(self.source,self.destination,repeats=1)
        (self.destination/"seed"/bench.slice_path(VA)).write_text(
            "// agent or external process changed the blank slice\n")
        with self.assertRaisesRegex(RuntimeError,"not the expected untouched blank slice"):
            bench.prepare(self.source,self.destination,repeats=1,resume_partial=True)
        self.assertFalse((self.destination/"plan.json").exists())

    def test_prepare_never_writes_live_repo_and_retains_vectors(self):
        with mock.patch.object(bench.subprocess,"run",
                               side_effect=AssertionError("Preparation ran a command")):
            bench.prepare(self.source,self.destination,repeats=2,minimum=7)
        plan=json.loads((self.destination/"plan.json").read_text())
        self.assertEqual(len(plan["arms"]),8)
        self.assertEqual(set(x["arm"] for x in plan["arms"]),set(bench.ARMS))
        self.assertEqual(set(x["state"] for x in plan["arms"]),{"prepared"})
        self.assertEqual((self.source/bench.slice_path(VA)).read_text(),self.original)
        self.assertIn("123",(self.destination/"archive/original-target.cpp").read_text())
        self.assertIn("blank-slate",(self.destination/"seed"/bench.slice_path(VA)).read_text())
        copied=(self.destination/"seed"/bench.RUNNER).read_text()
        self.assertIn("if not os.environ.get('SAD_BENCH_ACTIVE')",copied)
        self.assertIn("brain_trial_entry.py query",copied)
        self.assertTrue((self.destination/"seed"/bench.ENTRY).is_file())
        self.assertEqual(plan["protocol_version"],2)
        prompt=(self.destination/"seed"/bench.PROMPT).read_text()
        self.assertIn(bench.PROTOCOL_MARKER,prompt)
        self.assertIn("never return SKIP merely because of that claim",prompt)
        self.assertNotIn("if claimed, reply "+chr(96)+"SKIP: claimed"+chr(96),prompt)
        self.assertIn("if claimed, reply "+chr(96)+"SKIP: claimed"+chr(96),
                      (self.source/bench.PROMPT).read_text())
        with sqlite3.connect(self.source/bench.OPS) as original:
            self.assertEqual(original.execute(
                "SELECT COUNT(*) FROM attempts WHERE va=?",(VA,)).fetchone()[0],7)
        with sqlite3.connect(self.destination/"seed"/bench.OPS) as trial:
            self.assertEqual(trial.execute(
                "SELECT COUNT(*) FROM attempts").fetchone()[0],0)
            self.assertEqual(trial.execute(
                "SELECT COUNT(*) FROM agent_sessions").fetchone()[0],0)
        with sqlite3.connect(self.destination/"seed"/bench.KNOW) as mem:
            finding=mem.execute("SELECT va,title,evidence_path,source_hash FROM findings").fetchone()
            self.assertEqual(finding[0],plan["alias"])
            self.assertNotIn(VA,finding[1])
            self.assertEqual(finding[2],"")
            self.assertEqual(finding[3],"unchanged-digest")
            self.assertEqual(mem.execute(
                "SELECT embedding FROM finding_vectors WHERE rowid=1").fetchone()[0],
                "precomputed-384d-embedding")
            self.assertEqual(mem.execute("SELECT COUNT(*) FROM embedding_jobs").fetchone()[0],0)
            self.assertNotIn(VA,mem.execute(
                "SELECT title FROM compiler_techniques WHERE id=10").fetchone()[0])

    def test_absolute_live_symlink_retargeted_into_seed_and_each_trial(self):
        # The real Spore checkout has absolute links such as
        # work/match/scratch_s0088b250_inc/freetype -> /.../spore-decomp/...
        source_target=self.source/"src/engine/freetype"
        source_target.mkdir(parents=True)
        (source_target/"test.h").write_text("original source\n")
        link_rel=Path("work/match/scratch_s0088b250_inc/freetype")
        live_link=self.source/link_rel
        live_link.parent.mkdir(parents=True)
        live_link.symlink_to(source_target,target_is_directory=True)
        bench.prepare(self.source,self.destination,repeats=1)
        in_seed=self.destination/"seed"/link_rel
        self.assertTrue(in_seed.is_symlink())
        self.assertEqual(in_seed.resolve(),self.destination/"seed/src/engine/freetype")
        self.assertFalse(in_seed.resolve().is_relative_to(self.source))
        trial=self.destination/"trials/copied"
        bench._clone(self.destination/"seed",trial)
        in_trial=trial/link_rel
        self.assertTrue(in_trial.is_symlink())
        self.assertEqual(in_trial.resolve(),trial/"src/engine/freetype")
        (in_trial/"new-in-trial.h").write_text("trial only\n")
        self.assertFalse((source_target/"new-in-trial.h").exists())
        self.assertFalse((self.destination/"seed/src/engine/freetype/new-in-trial.h").exists())

    def test_resume_partial_symlink_failure_without_recoping_checkout(self):
        source_target=self.source/"src/freetype"
        source_target.mkdir(parents=True)
        link=self.source/"work/match/headers/freetype"
        link.parent.mkdir(parents=True)
        link.symlink_to(source_target,target_is_directory=True)
        with mock.patch.object(bench,"_relocate_symlinks",
                               side_effect=RuntimeError("simulate old symlink guard")):
            with self.assertRaisesRegex(RuntimeError,"simulate old symlink guard"):
                bench.prepare(self.source,self.destination,repeats=1)
        self.assertTrue((self.destination/"archive/spore.sqlite3").is_file())
        self.assertTrue((self.destination/"seed"/link.relative_to(self.source)).is_symlink())
        self.assertFalse((self.destination/"plan.json").exists())
        with mock.patch.object(bench.shutil,"copytree",
                               side_effect=AssertionError("Cannot recopy checkout on resume")):
            bench.prepare(self.source,self.destination,repeats=1,resume_partial=True)
        copied=self.destination/"seed"/link.relative_to(self.source)
        self.assertEqual(copied.resolve(),self.destination/"seed/src/freetype")
        self.assertTrue((self.destination/"plan.json").is_file())
        self.assertEqual((self.source/bench.slice_path(VA)).read_text(),self.original)

    def test_resume_partial_refuses_changed_source_without_touching_copy(self):
        with mock.patch.object(bench,"_relocate_symlinks",
                               side_effect=RuntimeError("simulate prior failure")):
            with self.assertRaises(RuntimeError):
                bench.prepare(self.source,self.destination,repeats=1)
        modified=(self.source/bench.slice_path(VA))
        modified.write_text("// changed by a live agent\n")
        with self.assertRaisesRegex(RuntimeError,"Live target changed"):
            bench.prepare(self.source,self.destination,repeats=1,resume_partial=True)
        self.assertFalse((self.destination/"plan.json").exists())

    def test_dry_run_cannot_charge_or_start_agent(self):
        bench.prepare(self.source,self.destination,repeats=1)
        with mock.patch.object(bench.subprocess,"run",
                               side_effect=AssertionError("Paid command invoked")):
            bench.run_next(self.destination,execute=False)
        self.assertFalse((self.destination/"trials").exists())
        plan=json.loads((self.destination/"plan.json").read_text())
        self.assertTrue(all(x["state"]=="prepared" for x in plan["arms"]))

    def test_mocked_paid_run_records_one_independent_trial_only(self):
        bench.prepare(self.source,self.destination,repeats=1)
        before=(self.source/bench.slice_path(VA)).read_bytes()
        def fake_agent(cmd,**kwargs):
            self.assertIn("--only-va",cmd)
            self.assertEqual(cmd[1],str(kwargs["cwd"]/bench.ENTRY))
            self.assertEqual(cmd[2],"runner")
            self.assertEqual(cmd[cmd.index("--only-va")+1],VA)
            self.assertEqual(kwargs["env"]["OPENROUTER_API_KEY"],"mock-not-a-real-key")
            self.assertEqual(kwargs["env"]["SAD_BENCH_ACTIVE"],"1")
            with sqlite3.connect(kwargs["cwd"]/bench.OPS) as db:
                db.execute("INSERT INTO attempts(va,status,note,utc) VALUES(?,?,?,?)",
                           (VA,"FAIL","checker: size=120/120 diffs=3",bench.now()))
            return mock.Mock(returncode=0)
        with mock.patch.dict("os.environ",{"OPENROUTER_API_KEY":"mock-not-a-real-key"}):
            with mock.patch.object(bench,"_start_vectors",return_value=None):
                with mock.patch.object(bench.subprocess,"run",side_effect=fake_agent) as runner:
                    bench.run_next(self.destination,execute=True,budget=.50)
        self.assertEqual(runner.call_count,1)
        plan=json.loads((self.destination/"plan.json").read_text())
        first=plan["arms"][0]
        self.assertEqual(first["state"],"completed")
        trial=self.destination/"trials"/first["name"]
        self.assertTrue(trial.exists())
        self.assertEqual((self.source/bench.slice_path(VA)).read_bytes(),before)
        with sqlite3.connect(self.source/bench.OPS) as live:
            self.assertEqual(live.execute(
                "SELECT COUNT(*) FROM attempts WHERE va=?",(VA,)).fetchone()[0],7)
        bench.report(self.destination)
        data=json.loads((self.destination/"report.json").read_text())
        row=next(x for x in data["trials"] if x["name"]==first["name"])
        self.assertFalse(row["match"])
        self.assertEqual(row["diff_bytes"],3)

    def test_zero_evidence_runner_failure_is_unscored_and_requeue_preserves_log(self):
        bench.prepare(self.source,self.destination,repeats=1)
        with mock.patch.dict("os.environ",{"OPENROUTER_API_KEY":"mock-not-a-real-key"}):
            with mock.patch.object(bench,"_start_vectors",return_value=None):
                with mock.patch.object(bench.subprocess,"run",
                                       return_value=mock.Mock(returncode=1)) as runner:
                    bench.run_next(self.destination,execute=True,budget=.50)
        self.assertEqual(runner.call_count,1)
        plan=json.loads((self.destination/"plan.json").read_text())
        item=plan["arms"][0]
        old_name=item["name"]
        self.assertEqual(item["state"],"error")
        logfile=self.destination/"trials"/old_name/"benchmark-run.log"
        self.assertTrue(logfile.is_file())
        bench.report(self.destination)
        data=json.loads((self.destination/"report.json").read_text())
        result=next(x for x in data["trials"] if x["name"]==old_name)
        self.assertIsNone(result["match"])
        self.assertEqual(result["status"],"NO_RESULT")
        bench.retry_invalid(self.destination)
        plan=json.loads((self.destination/"plan.json").read_text())
        refreshed=plan["arms"][0]
        self.assertNotEqual(refreshed["name"],old_name)
        self.assertEqual(refreshed["state"],"prepared")
        self.assertEqual(refreshed["invalid_history"][0]["name"],old_name)
        self.assertTrue(logfile.is_file())
        self.assertFalse((self.destination/"trials"/refreshed["name"]).exists())
        with mock.patch.object(bench.subprocess,"run",
                               side_effect=AssertionError("Dry-run tried to invoke agent")):
            bench.run_next(self.destination,execute=False)
        with self.assertRaisesRegex(RuntimeError,"exactly one"):
            bench.retry_invalid(self.destination)

    def test_invalid_trial_with_agent_evidence_cannot_be_requeued(self):
        bench.prepare(self.source,self.destination,repeats=1)
        with mock.patch.dict("os.environ",{"OPENROUTER_API_KEY":"mock-not-a-real-key"}):
            with mock.patch.object(bench,"_start_vectors",return_value=None):
                with mock.patch.object(bench.subprocess,"run",
                                       return_value=mock.Mock(returncode=1)):
                    bench.run_next(self.destination,execute=True,budget=.50)
        plan=json.loads((self.destination/"plan.json").read_text())
        trial=self.destination/"trials"/plan["arms"][0]["name"]
        audit=trial/bench.WORK/"logs/attempt.jsonl"
        audit.parent.mkdir(parents=True,exist_ok=True)
        audit.write_text('{"type":"tool_use"}\n')
        with self.assertRaisesRegex(RuntimeError,"JSONL evidence"):
            bench.retry_invalid(self.destination)
        self.assertEqual(json.loads((self.destination/"plan.json").read_text())["arms"][0]["state"],
                         "error")

    def test_zero_exit_without_recorded_attempt_is_invalid(self):
        bench.prepare(self.source,self.destination,repeats=1)
        with mock.patch.dict("os.environ",{"OPENROUTER_API_KEY":"mock-not-a-real-key"}):
            with mock.patch.object(bench,"_start_vectors",return_value=None):
                with mock.patch.object(bench.subprocess,"run",
                                       return_value=mock.Mock(returncode=0)):
                    bench.run_next(self.destination,execute=True,budget=.50)
        plan=json.loads((self.destination/"plan.json").read_text())
        self.assertEqual(plan["arms"][0]["state"],"error")
        bench.report(self.destination)
        matches=[x["match"] for x in
                 json.loads((self.destination/"report.json").read_text())["trials"]]
        self.assertTrue(all(x is None for x in matches))

    def test_isolation_refuses_source_parent_and_existing_output(self):
        with self.assertRaises(ValueError):
            bench.prepare(self.source,self.source/"inside",repeats=1)
        self.destination.mkdir()
        with self.assertRaises(FileExistsError):
            bench.prepare(self.source,self.destination,repeats=1)

    def test_clone_is_independent_and_does_not_mutate_seed(self):
        bench.prepare(self.source,self.destination,repeats=1)
        trial=self.destination/"trials"/"cloned"
        bench._clone(self.destination/"seed",trial)
        self.assertTrue((trial/bench.KNOW).exists())
        (trial/bench.slice_path(VA)).write_text("// changed by agent\n")
        self.assertIn("blank-slate",(self.destination/"seed"/bench.slice_path(VA)).read_text())
        with sqlite3.connect(trial/bench.OPS) as db:
            db.execute("INSERT INTO attempts(va,status,note,utc) VALUES(?,?,?,?)",
                       (VA,"FAIL","trial only",bench.now()))
        with sqlite3.connect(self.destination/"seed"/bench.OPS) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0],0)

    def test_report_skip_is_not_a_byte_mismatch_or_knowledge_exposure(self):
        bench.prepare(self.source,self.destination,repeats=1)
        plan=json.loads((self.destination/"plan.json").read_text())
        raw=next(item for item in plan["arms"] if item["arm"]=="raw")
        trial=self.destination/"trials"/raw["name"]
        bench._clone(self.destination/"seed",trial)
        with sqlite3.connect(trial/bench.OPS) as db:
            db.execute("INSERT INTO attempts(va,status,note,utc) VALUES(?,?,?,?)",
                       (VA,"SKIP","Pass 1: shared-header change required | checker: NO MATCH",bench.now()))
        raw["state"]="completed"
        bench.write_json(self.destination/"plan.json",plan)
        bench.report(self.destination)
        rows=json.loads((self.destination/"report.json").read_text())["trials"]
        actual=next(item for item in rows if item["name"]==raw["name"])
        self.assertEqual(actual["status"],"SKIP")
        self.assertEqual(actual["match"],False)
        self.assertIn("shared-header change required",actual["result_note"])
        self.assertEqual(actual["exposure"],"not_queried")
        self.assertFalse(actual["treatment_adherent"])
        self.assertEqual(actual["brain_queries"],0)
        self.assertEqual(actual["checker_calls"],0)

    def test_exposure_requires_actual_returned_findings_and_techniques(self):
        audit=self.base/"brain-queries.jsonl"
        self.assertEqual(bench._knowledge_exposure("raw",audit)["exposure"],"not_queried")
        self.assertTrue(bench._knowledge_exposure("baseline",audit)["adherent"])
        audit.write_text('{"findings":0,"techniques":0,"characters":100}\n')
        self.assertFalse(bench._knowledge_exposure("raw",audit)["adherent"])
        self.assertFalse(bench._knowledge_exposure("consolidated",audit)["adherent"])
        self.assertTrue(bench._knowledge_exposure("placebo",audit)["adherent"])
        with audit.open("a") as h:
            h.write('{"findings":5,"techniques":2,"characters":2500}\n')
        raw=bench._knowledge_exposure("raw",audit)
        self.assertEqual(raw["findings"],5)
        self.assertTrue(raw["adherent"])
        cons=bench._knowledge_exposure("consolidated",audit)
        self.assertEqual(cons["techniques"],2)
        self.assertTrue(cons["adherent"])

    def test_report_no_results_is_not_claimed_success(self):
        bench.prepare(self.source,self.destination,repeats=1)
        bench.report(self.destination)
        rows=json.loads((self.destination/"report.json").read_text())["trials"]
        self.assertEqual(len(rows),4)
        self.assertTrue(all(x["match"] is None for x in rows))

    def test_protect_multiple_function_markers_in_same_slice(self):
        cpp=self.source/bench.slice_path(VA)
        cpp.write_text(self.original+f"\n// @ {OTHER}\n")
        with self.assertRaises(ValueError):
            bench.prepare(self.source,self.destination,repeats=1)
        self.assertEqual(cpp.read_text(),self.original+f"\n// @ {OTHER}\n")


if __name__=="__main__":
    unittest.main()
