"""Offline tests for matched-function study isolation and one-command execution."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

import brain_ablation as brain
import brain_ablation_suite as suite
from test_brain_ablation import BrainAblationTests, VA


class SolvedSuiteTests(unittest.TestCase):
    def setUp(self):
        fixture=BrainAblationTests("test_dry_run_cannot_charge_or_start_agent")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.base=fixture.base
        self.source=fixture.source
        self.destination=fixture.destination
        self.original=fixture.original
        self.targets=suite.TARGET_CANDIDATES[:3]
        self.out=self.base/"solved-suite"
        (self.source/brain.TASK).parent.mkdir(parents=True,exist_ok=True)
        (self.source/brain.TASK).write_text("""import os
def main(va):
    cl, prs = claims(max_age=0), open_prs(va)
    if st != "byte-exact":
        print("historical solutions")
""")
        with sqlite3.connect(self.source/brain.OPS) as db:
            db.execute("DELETE FROM attempts")
            db.execute("DELETE FROM queue")
            db.execute("DELETE FROM agent_sessions")
            for va in self.targets:
                db.execute("INSERT INTO queue VALUES(?,?)",(va,128))
                for i in range(4):
                    db.execute("INSERT INTO attempts(va,status,note,utc) VALUES(?,?,?,?)",
                               (va,"FAIL",f"Attempt {i}",brain.now()))
                db.execute("INSERT INTO attempts(va,status,note,utc) VALUES(?,?,?,?)",
                           (va,"MATCH","independently checker verified",brain.now()))
        for va in self.targets:
            cpp=self.source/brain.slice_path(va)
            cpp.parent.mkdir(parents=True)
            cpp.write_text(f"// @ {va}\nint solved_{va[2:]}() {{ return 42; }}\n")
            (cpp.parent/"manifest.txt").write_text(
                f"s{va[2:]}/s{va[2:]}.cpp  ?FUN_{va[2:]}@@YAXXZ  {va[2:]}  /O2 /MD\n")
            (self.source/"work"/f"task_closed_{va[2:]}.json").write_text(
                f'Solved {va}: return 42\n')
        with sqlite3.connect(self.source/brain.KNOW) as db:
            for i,va in enumerate(self.targets,start=2):
                db.execute("INSERT INTO findings VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (i,va,5,"MATCH",1,f"Solution {va}","return 42",
                            f"original C++ {va}","old-source-path","hash"))
                db.execute("INSERT INTO findings_fts(rowid,title,summary,details) VALUES(?,?,?,?)",
                           (i,f"Solution {va}","return 42",f"original C++ {va}"))
                db.execute("INSERT INTO finding_vectors VALUES(?,?)",
                           (i,f"secret-embedding-{va}"))
                db.execute("INSERT INTO compiler_techniques VALUES(?,?,?,?,?,?,?)",
                           (20+i,"Exact original solution","compiler",
                            f"Derived from {va}","condition","intervention","result"))
                db.execute("INSERT INTO compiler_techniques_fts(rowid,title,category,problem,conditions,intervention,observed_effect) VALUES(?,?,?,?,?,?,?)",
                           (20+i,"Exact original solution","compiler",
                            f"Derived from {va}","condition","intervention","result"))
                db.execute("INSERT INTO compiler_technique_links VALUES(?,?,?,?)",
                           (20+i,i,f"Solution reference {va}","observed match"))
            # Add one unrelated finding and an unrelated technique that MUST survive.
            db.execute("INSERT INTO findings VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (10,"0x00201020",1,"FAIL",0,"Unrelated ABI",
                        "MSVC stack frame","not about test set","","otherhash"))
            db.execute("INSERT INTO findings_fts(rowid,title,summary,details) VALUES(?,?,?,?)",
                       (10,"Unrelated ABI","MSVC stack frame","not about test set"))
            db.execute("INSERT INTO compiler_techniques VALUES(?,?,?,?,?,?,?)",
                       (100,"Other technique","ABI","independent","x86","try","works"))
            db.execute("INSERT INTO compiler_techniques_fts(rowid,title,category,problem,conditions,intervention,observed_effect) VALUES(?,?,?,?,?,?,?)",
                       (100,"Other technique","ABI","independent","x86","try","works"))

    def test_history_selects_only_failures_before_verified_match(self):
        found={r["va"]:r for r in brain.known_solutions(self.source,3)}
        self.assertEqual(set(found),set(self.targets))
        self.assertTrue(all(r["failures"]==4 for r in found.values()))
        with sqlite3.connect(self.source/brain.OPS) as db:
            first=db.execute("SELECT id FROM attempts WHERE va=? ORDER BY id LIMIT 1",
                             (self.targets[0],)).fetchone()[0]
            db.execute("UPDATE attempts SET status='DEFER' WHERE id=?",(first,))
        self.assertEqual(brain.known_solutions(self.source,4)[0]["failures"],4)

    def test_suite_prepare_isolation_wipes_all_three_holdouts(self):
        with mock.patch.object(suite.brain,"_start_vectors",
                               side_effect=AssertionError("preparation started vectors")):
            suite.prepare(self.source,self.out,repeats=1)
        plan=json.loads((self.out/"suite-plan.json").read_text())
        self.assertEqual(len(plan["schedule"]),12)
        self.assertEqual(set(plan["targets"]),set(self.targets))
        frozen=self.out/"frozen"/"knowledge.sqlite3"
        self.assertTrue(frozen.is_file())
        master_hash=brain._tool_hash(frozen)
        self.assertEqual(master_hash,plan["frozen_knowledge_sha256"])
        for va in self.targets:
            sub=suite._target_dir(self.out,va)
            self.assertTrue((sub/"plan.json").is_file())
            self.assertEqual(brain._tool_hash(sub/"archive/knowledge.sqlite3"),master_hash)
            inner=json.loads((sub/"plan.json").read_text())
            self.assertTrue(inner["solved_cohort"])
            self.assertEqual(inner["holdouts"],sorted(self.targets))
            self.assertEqual(inner["knowledge_pruning"]["removed_findings"],3)
            self.assertEqual(inner["knowledge_pruning"]["removed_techniques"],3)
            task=(sub/"seed"/brain.TASK).read_text()
            self.assertIn(brain.TASK_MARKER,task)
            self.assertIn("not os.environ.get",task)
            for held in self.targets:
                self.assertNotIn("return 42",
                                 (sub/"seed"/brain.slice_path(held)).read_text())
                self.assertIn("Controlled blank-slate experiment",
                              (sub/"seed"/brain.slice_path(held)).read_text())
                manifest=(sub/"seed"/brain.slice_path(held)).parent/"manifest.txt"
                self.assertNotIn(held[2:],manifest.read_text())
                self.assertFalse((sub/"seed"/"work"/f"task_closed_{held[2:]}.json").exists())
            with sqlite3.connect(sub/"seed"/brain.KNOW) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM findings").fetchone()[0],2)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM compiler_techniques").fetchone()[0],2)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM finding_vectors").fetchone()[0],1)
                fts=db.execute("SELECT COUNT(*) FROM findings_fts").fetchone()[0]
                self.assertEqual(fts,2)
            with sqlite3.connect(sub/"seed"/brain.OPS) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0],0)
            self.assertEqual((sub/"archive/original-target.cpp").read_text(),
                             (self.source/brain.slice_path(va)).read_text())
        self.assertIn("return 42",(self.source/brain.slice_path(self.targets[0])).read_text())

    def test_one_command_runs_every_arm_and_resumes_without_paying_again(self):
        def fake_paid(out,execute,budget,minutes):
            self.assertTrue(execute)
            self.assertEqual(budget,.5)
            plan=json.loads((out/"plan.json").read_text())
            item=next(r for r in plan["arms"] if r["state"]=="prepared")
            trial=out/"trials"/item["name"]
            brain._clone(out/"seed",trial)
            with sqlite3.connect(trial/brain.OPS) as db:
                db.execute("INSERT INTO attempts(va,status,note,utc) VALUES(?,?,?,?)",
                           (plan["target"],"MATCH","independent byte-exact verification",brain.now()))
            item["state"]="completed"
            brain.write_json(out/"plan.json",plan)
        with mock.patch.object(suite.brain,"run_next",side_effect=fake_paid) as fake:
            result=suite.run(self.source,self.out,repeats=1,execute=True,budget=.5)
            self.assertEqual(fake.call_count,12)
            self.assertEqual(sum(int(t["match"]) for t in result["trials"]),12)
            second=suite.run(self.source,self.out,repeats=1,execute=True,budget=.5)
            self.assertEqual(fake.call_count,12)
            self.assertEqual(sum(int(t["match"]) for t in second["trials"]),12)
        self.assertTrue((self.out/"suite-results.csv").exists())
        self.assertTrue((self.out/"suite-report.json").exists())
        self.assertEqual(len(result["trials"]),12)
        self.assertEqual({r["arm"] for r in result["trials"]},set(brain.ARMS))

    def test_default_run_only_prepares_and_never_calls_agent(self):
        with mock.patch.object(suite.brain,"run_next",
                               side_effect=AssertionError("agent was invoked")):
            result=suite.run(self.source,self.out,repeats=1)
        self.assertEqual(len(result["trials"]),12)
        self.assertTrue(all(not x["valid"] for x in result["trials"]))

    def test_multi_function_source_is_rejected_before_spending(self):
        offender=self.targets[2]
        path=self.source/brain.slice_path(offender)
        with path.open("a") as f:
            f.write("// @ 0x00400040\nvoid unrelated() {}\n")
        with mock.patch.object(suite.brain,"run_next",
                               side_effect=AssertionError("paid agent started")):
            with self.assertRaisesRegex(RuntimeError,"Only 2 of 3 required"):
                suite.run(self.source,self.out,repeats=1,execute=True)
        self.assertFalse((self.out/"functions").exists())

    def _legacy_provider_fixture(self, *, contaminated=False):
        """A fully prepared suite with exactly one 404 before model tokens."""
        suite.prepare(self.source,self.out,repeats=1)
        top=self.out/"suite-plan.json"
        master=json.loads(top.read_text())
        master.pop("provider_routing",None)
        brain.write_json(top,master)
        for va in master["targets"]:
            sub=suite._target_dir(self.out,va)
            planpath=sub/"plan.json"
            inner=json.loads(planpath.read_text())
            runner=sub/"seed"/brain.RUNNER
            code=runner.read_text()
            self.assertIn(brain.NEW_PROVIDER_ROUTE,code)
            runner.write_text(code.replace(
                brain.NEW_PROVIDER_ROUTE,brain.OLD_PROVIDER_ROUTE))
            inner.pop("provider_routing",None)
            inner["tooling_sha256"][brain.RUNNER.as_posix()]=brain._tool_hash(runner)
            brain.write_json(planpath,inner)
        first=master["schedule"][0]
        sub=suite._target_dir(self.out,first)
        planpath=sub/"plan.json"
        inner=json.loads(planpath.read_text())
        item=inner["arms"][0]
        oldname=item["name"]
        trial=sub/"trials"/oldname
        brain._clone(sub/"seed",trial)
        logpath=trial/brain.WORK/"logs"/(
            first[2:]+".deepseek-openrouter.test.jsonl")
        logpath.parent.mkdir(parents=True,exist_ok=True)
        events=[
            {"type":"step_start"},
            {"type":"error","error":{
                "type":"provider.invalid-request","status":404,
                "message":"No endpoints found for "+inner["model"]+
                ". Every candidate endpoint was removed during routing: "
                "Filter by Fallback removed other providers",
            }}
        ]
        if contaminated:
            events.insert(1,{"type":"tool_use","part":{"state":{
                "input":{"command":"tools/contrib/check.py"}}}})
        logpath.write_text("\n".join(json.dumps(x) for x in events)+"\n")
        (trial/"benchmark-run.log").write_text(
            "OpenRouter charged in THIS run: $0.0000 / $0.50\n"
            "openrouter error, stopping without ticking "+first+"\n")
        item["state"]="error"
        item["exit_code"]=0
        item["error"]="Runner exited without a recorded decompilation attempt"
        brain.write_json(planpath,inner)
        return master,first,oldname,logpath

    def test_privacy_provider_config_preserves_model_and_disallows_training(self):
        suite.prepare(self.source,self.out,repeats=1)
        for va in self.targets:
            sub=suite._target_dir(self.out,va)
            runner=(sub/"seed"/brain.RUNNER).read_text()
            self.assertIn(brain.ROUTING_MARKER,runner)
            self.assertIn("'data_collection': 'deny'",runner)
            self.assertIn("'allow_fallbacks': False",runner)
            self.assertIn("'nebius/fp4'",runner)
            self.assertIn("'relace/fp4'",runner)
            self.assertIn("'stepfun/fp8'",runner)
            self.assertIn("'max_price': {'prompt': 0.18, 'completion': 0.72}",runner)
            self.assertNotIn(brain.OLD_PROVIDER_ROUTE,runner)
            inner=json.loads((sub/"plan.json").read_text())
            self.assertEqual(inner["provider_routing"],brain.ROUTING_MARKER)
            self.assertEqual(inner["model"],"deepseek/deepseek-v4.1-flash-1017")

    def test_previous_privacy_fallback_suite_upgrades_without_paid_work(self):
        suite.prepare(self.source,self.out,repeats=1)
        top=self.out/"suite-plan.json"
        master=json.loads(top.read_text())
        master["provider_routing"]=brain.LEGACY_ROUTING_MARKER
        brain.write_json(top,master)
        for va in master["targets"]:
            sub=suite._target_dir(self.out,va)
            path=sub/"plan.json"
            inner=json.loads(path.read_text())
            runner=sub/"seed"/brain.RUNNER
            code=runner.read_text()
            self.assertIn(brain.NEW_PROVIDER_ROUTE,code)
            runner.write_text(code.replace(brain.NEW_PROVIDER_ROUTE,
                                           brain.LEGACY_PROVIDER_ROUTE))
            inner["provider_routing"]=brain.LEGACY_ROUTING_MARKER
            inner["tooling_sha256"][brain.RUNNER.as_posix()]=brain._tool_hash(runner)
            brain.write_json(path,inner)
        with mock.patch.object(suite.brain,"run_next",
                               side_effect=AssertionError("paid agent started")):
            suite.run(self.source,self.out,repeats=1,execute=False)
        self.assertEqual(json.loads(top.read_text())["provider_routing"],
                         brain.ROUTING_MARKER)
        for va in master["targets"]:
            sub=suite._target_dir(self.out,va)
            inner=json.loads((sub/"plan.json").read_text())
            self.assertEqual(inner["provider_routing"],brain.ROUTING_MARKER)
            self.assertIn(brain.NEW_PROVIDER_ROUTE,
                          (sub/"seed"/brain.RUNNER).read_text())
            brain._seed_tooling(sub,inner)

    def test_preexisting_v2_suite_upgrades_only_when_unmeasured(self):
        suite.prepare(self.source,self.out,repeats=1)
        top=self.out/"suite-plan.json"
        master=json.loads(top.read_text())
        master["provider_routing"]=brain.PREVIOUS_ROUTING_MARKER
        brain.write_json(top,master)
        for va in master["targets"]:
            sub=suite._target_dir(self.out,va)
            path=sub/"plan.json"
            inner=json.loads(path.read_text())
            runner=sub/"seed"/brain.RUNNER
            source=runner.read_text()
            self.assertIn(brain.NEW_PROVIDER_ROUTE,source)
            runner.write_text(source.replace(brain.NEW_PROVIDER_ROUTE,
                                             brain.PREVIOUS_PROVIDER_ROUTE))
            inner["provider_routing"]=brain.PREVIOUS_ROUTING_MARKER
            inner["tooling_sha256"][brain.RUNNER.as_posix()]=brain._tool_hash(runner)
            brain.write_json(path,inner)
        # No external inference is permitted during a policy upgrade.
        with mock.patch.object(suite.brain,"run_next",
                               side_effect=AssertionError("paid agent started")):
            suite.run(self.source,self.out,repeats=1,execute=False)
        self.assertEqual(json.loads(top.read_text())["provider_routing"],brain.ROUTING_MARKER)
        for va in master["targets"]:
            sub=suite._target_dir(self.out,va)
            inner=json.loads((sub/"plan.json").read_text())
            self.assertEqual(inner["provider_routing"],brain.ROUTING_MARKER)
            self.assertIn(brain.NEW_PROVIDER_ROUTE,
                          (sub/"seed"/brain.RUNNER).read_text())
            brain._seed_tooling(sub,inner)

    def test_v2_upgrade_refuses_active_trial_without_modification(self):
        suite.prepare(self.source,self.out,repeats=1)
        top=self.out/"suite-plan.json"
        master=json.loads(top.read_text())
        master["provider_routing"]=brain.PREVIOUS_ROUTING_MARKER
        brain.write_json(top,master)
        snapshots=[]
        for va in master["targets"]:
            sub=suite._target_dir(self.out,va)
            path=sub/"plan.json"
            inner=json.loads(path.read_text())
            runner=sub/"seed"/brain.RUNNER
            runner.write_text(runner.read_text().replace(brain.NEW_PROVIDER_ROUTE,
                                                          brain.PREVIOUS_PROVIDER_ROUTE))
            inner["provider_routing"]=brain.PREVIOUS_ROUTING_MARKER
            inner["tooling_sha256"][brain.RUNNER.as_posix()]=brain._tool_hash(runner)
            brain.write_json(path,inner)
            snapshots.append((runner,runner.read_bytes()))
        first=master["targets"][0]
        first_plan=suite._target_dir(self.out,first)/"plan.json"
        current=json.loads(first_plan.read_text())
        current["arms"][0]["state"]="running"
        brain.write_json(first_plan,current)
        with mock.patch.object(suite.brain,"run_next",
                               side_effect=AssertionError("paid agent started")):
            with self.assertRaisesRegex(RuntimeError,"An agent may have run"):
                suite.run(self.source,self.out,repeats=1,execute=False)
        self.assertEqual(json.loads(top.read_text())["provider_routing"],
                         brain.PREVIOUS_ROUTING_MARKER)
        for runner,original in snapshots:
            self.assertEqual(runner.read_bytes(),original)

    def test_legacy_suite_requeues_only_pre_model_provider_404(self):
        master,first,oldname,logpath=self._legacy_provider_fixture()
        old_sha=brain._tool_hash(logpath)
        with mock.patch.object(brain,"run_next",
                               side_effect=AssertionError("No paid agent allowed")):
            result=suite.run(self.source,self.out,repeats=1,execute=False)
        self.assertTrue(logpath.is_file())
        self.assertEqual(brain._tool_hash(logpath),old_sha)
        self.assertEqual(len(result["trials"]),12)
        after=json.loads((self.out/"suite-plan.json").read_text())
        self.assertEqual(after["provider_routing"],brain.ROUTING_MARKER)
        self.assertEqual(after["frozen_knowledge_sha256"],master["frozen_knowledge_sha256"])
        oldsub=suite._target_dir(self.out,first)
        inner=json.loads((oldsub/"plan.json").read_text())
        retried=inner["arms"][0]
        self.assertEqual(retried["state"],"prepared")
        self.assertNotEqual(retried["name"],oldname)
        self.assertEqual(retried["invalid_history"][-1]["name"],oldname)
        self.assertEqual(retried["invalid_history"][-1]["evidence"]["error_status"],404)
        for va in self.targets:
            sub=suite._target_dir(self.out,va)
            inner=json.loads((sub/"plan.json").read_text())
            self.assertEqual(inner["provider_routing"],brain.ROUTING_MARKER)
            self.assertEqual(inner["source_knowledge_sha256"],
                             master["frozen_knowledge_sha256"])
            brain._seed_tooling(sub,inner)
        with mock.patch.object(brain,"run_next",
                               side_effect=AssertionError("No model call")):
            suite.run(self.source,self.out,repeats=1,execute=False)
        again=json.loads((oldsub/"plan.json").read_text())
        self.assertEqual(again["arms"][0]["name"],retried["name"])

    def test_legacy_suite_refuses_any_model_or_tool_activity(self):
        _,_,name,logpath=self._legacy_provider_fixture(contaminated=True)
        with self.assertRaisesRegex(RuntimeError,"model output, tools"):
            suite.run(self.source,self.out,repeats=1,execute=False)
        oldsub=suite._target_dir(self.out,json.loads(
            (self.out/"suite-plan.json").read_text())["schedule"][0])
        inner=json.loads((oldsub/"plan.json").read_text())
        self.assertEqual(inner["arms"][0]["name"],name)
        self.assertIsNone(inner.get("provider_routing"))
        self.assertTrue(logpath.is_file())

    def test_missing_match_history_fails_before_preparation(self):
        with sqlite3.connect(self.source/brain.OPS) as db:
            db.execute("DELETE FROM attempts WHERE va=? AND status='MATCH'",
                       (self.targets[0],))
        with self.assertRaisesRegex(RuntimeError,"Only 2 of 3 required"):
            suite.prepare(self.source,self.out,repeats=1)
        self.assertFalse((self.out/"functions").exists())


if __name__=="__main__":
    unittest.main()
