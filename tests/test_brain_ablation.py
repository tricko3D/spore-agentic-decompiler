"""Offline safety and fidelity tests for the four-arm Brain ablation harness."""
from __future__ import annotations

import importlib.util
import json
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
        (self.source/bench.RUNNER).write_text("""import os
def run_one():
    prompt = "source"
    if collective_memory.paths(ROOT)[2].is_file():
        prompt += "brain"
    if os.environ.get('SPORE_SQLITE_RUN_ID'):
        prompt += f\"\"\"PARALLEL\"\"\"
""",encoding="utf-8")
        (self.source/bench.MEMORY).write_text("# fake local memory agent\n",encoding="utf-8")
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
        self.assertIn("if not os.environ.get('SAD_BENCH_ARM')",copied)
        self.assertIn("brain_trial_query.py",copied)
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
            self.assertEqual(cmd[cmd.index("--only-va")+1],VA)
            self.assertEqual(kwargs["env"]["OPENROUTER_API_KEY"],"mock-not-a-real-key")
            self.assertIn(kwargs["env"]["SAD_BENCH_ARM"],bench.ARMS)
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
