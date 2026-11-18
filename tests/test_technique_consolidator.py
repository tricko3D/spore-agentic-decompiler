"""SAD compiler technique consolidation, fully offline mocked API tests."""
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "engine/spore_provider_fix/work/scratch/spore_technique_consolidator.py"
spec = importlib.util.spec_from_file_location("sad_consolidator_tests", SOURCE)
tech = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tech)

VA = "0x00e61b90"
OTHER = "0x00d40930"
EVIDENCE = ("Retail enters with EAX=pos and EDX=effectId "
            "instead of ordinary stack-based arguments.")


class ConsolidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        tech.memory.initialize(self.root)
        tech.ensure(self.root)
        tech.memory.ingest(
            self.root, VA, 1, "FAIL", note=EVIDENCE,
            checker="NO MATCH size 532, retail 499")
        self.assertEqual(tech.ensure(self.root), None)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def proposal(action="create", existing_id=None, title="Custom EAX EDX register ABI"):
        return {
            "action":action, "existing_id":existing_id,
            "title":title, "category":"calling_convention",
            "problem":"Retail uses an internal register-based ABI",
            "conditions":"32-bit MSVC retail binary, effect helper",
            "intervention":"Same translation unit and static helper tried",
            "observed_effect":"Compiled size 532 versus 499, not byte-exact",
            "evidence_quote":EVIDENCE
        }

    def fake_api(self, output=None):
        events = []
        def call(key, user):
            events.append((key, user))
            return (output if output is not None else
                    {"techniques":[self.proposal()]}), {
                       "cost":0.0009,"prompt_tokens":400,"completion_tokens":140}
        return call, events

    def test_initial_queue_and_no_paid_work_during_setup(self):
        state = tech.status(self.root)
        self.assertEqual(state["queue"]["pending"], 1)
        self.assertEqual(state["techniques"], 0)
        self.assertEqual(state["reserved_or_spent_today_usd"], 0)
        self.assertEqual(tech.synchronize(tech.memory.connection(self.root)), 0)

    def test_process_creates_grounded_technique_and_links_source(self):
        fake,calls = self.fake_api()
        outcome = tech.process(self.root,max_items=1,daily_usd=0.15,caller=fake,key="test")
        self.assertEqual(outcome["processed"], 1)
        self.assertEqual(outcome["technique_links_added"], 1)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("OPENROUTER_API_KEY", calls[0][1])
        self.assertEqual(tech.status(self.root)["queue"].get("done"), 1)
        results = tech.search(self.root,"EAX register based ABI",2)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["sources"][0]["va"], VA)
        self.assertEqual(results[0]["sources"][0]["status"], "FAIL")
        self.assertEqual(results[0]["evidence_level"], "observed")
        self.assertNotIn("verified", tech.format_techniques(results).lower())
        self.assertEqual(tech.process(self.root, max_items=1, caller=fake,key="test")["processed"],0)

    def test_reject_hallucinated_evidence_and_do_not_store_technique(self):
        bad = self.proposal()
        bad["evidence_quote"] = "Nothing like this was actually seen by any compiler"
        fake,_ = self.fake_api({"techniques":[bad]})
        result = tech.process(self.root,caller=fake,key="test")
        self.assertEqual(result["failed"],1)
        self.assertEqual(tech.status(self.root)["techniques"],0)
        self.assertEqual(tech.status(self.root)["queue"]["pending"],1)
        # Even a malformed response reserves an amount, ensuring budget cannot
        # be bypassed with repeated unsuccessful provider requests.
        self.assertGreater(tech.status(self.root)["reserved_or_spent_today_usd"],0)

    def test_malformed_response_does_not_block_other_findings(self):
        tech.memory.ingest(self.root, OTHER, 1, "FAIL",
                           note="different stack frame layout and register pressure",
                           checker="NO MATCH 492/488")
        call_count = []
        def partial_failure(key, prompt):
            call_count.append(prompt)
            if len(call_count) == 1:
                invalid = self.proposal()
                invalid["evidence_quote"] = "Invented source that was never present"
                return {"techniques":[invalid]}, {
                    "cost":0.0009,"prompt_tokens":400,"completion_tokens":140}
            return {"techniques":[]}, {
                "cost":0.0007,"prompt_tokens":350,"completion_tokens":50}
        result = tech.process(self.root,max_items=2,daily_usd=0.25,
                              caller=partial_failure,key="test")
        self.assertEqual(result["processed"],1)
        self.assertEqual(result["failed"],1)
        self.assertEqual(len(call_count),2)
        self.assertEqual(len(result["errors"]),1)
        self.assertIn("Unsubstantiated",result["errors"][0]["reason"])
        self.assertEqual(result["stats"]["queue"]["pending"],1)
        self.assertEqual(result["stats"]["queue"]["done"],1)
        self.assertTrue(result["stats"]["recent_errors"])
        # Invalid DeepSeek JSON is charged at returned actual cost,
        # not the full $0.01 conservative reservation.
        with tech.memory.connection(self.root) as con:
            usage = con.execute("SELECT SUM(cost_usd) FROM technique_spend").fetchone()[0]
        self.assertAlmostEqual(usage,0.0016)

    def test_failed_finding_cools_down_and_is_not_immediately_retried(self):
        bad = self.proposal()
        bad["evidence_quote"] = "Completely unsupported invented text"
        fake,calls = self.fake_api({"techniques":[bad]})
        first = tech.process(self.root,max_items=10,caller=fake,key="test")
        self.assertEqual(first["failed"],1)
        self.assertEqual(len(calls),1)
        self.assertEqual(tech.process(self.root,max_items=10,caller=fake,
                                      key="test")["processed"],0)
        self.assertEqual(len(calls),1)
        with tech.memory.connection(self.root) as con:
            row = con.execute(
                "SELECT next_retry_utc,failures FROM technique_queue").fetchone()
            self.assertIsNotNone(row["next_retry_utc"])
            self.assertEqual(row["failures"],1)

    def test_existing_database_migrates_retry_field_without_losing_queue(self):
        with tech.memory.connection(self.root) as con:
            # Reproduce the old schema by reconstructing its queue table
            # before running the idempotent ensure() migration.
            con.execute("DROP TABLE technique_queue")
            con.execute("""CREATE TABLE technique_queue (
                finding_id INTEGER PRIMARY KEY REFERENCES findings(id),
                source_hash TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                failures INTEGER NOT NULL DEFAULT 0,
                lease_utc TEXT,updated_utc TEXT NOT NULL,error TEXT NOT NULL DEFAULT ''
            )""")
            con.execute("""INSERT INTO technique_queue
                (finding_id,source_hash,state,updated_utc)
                SELECT id,source_hash,'pending',? FROM findings""",(tech.now(),))
        tech.ensure(self.root)
        with tech.memory.connection(self.root) as con:
            cols = {col["name"] for col in con.execute("PRAGMA table_info(technique_queue)")}
            count = con.execute("SELECT COUNT(*) FROM technique_queue").fetchone()[0]
        self.assertIn("next_retry_utc",cols)
        self.assertEqual(count,1)

    def test_empty_techniques_is_valid_and_idempotent(self):
        fake,_ = self.fake_api({"techniques":[]})
        result = tech.process(self.root,caller=fake,key="test")
        self.assertEqual(result["processed"],1)
        self.assertEqual(tech.status(self.root)["techniques"],0)
        self.assertEqual(tech.status(self.root)["queue"]["done"],1)

    def test_budget_limits_number_of_calls_and_separate_ledger(self):
        tech.memory.ingest(self.root, OTHER,1,"FAIL",note="stack register allocation differs",
                           checker="NO MATCH size 492, retail 488")
        fake,calls = self.fake_api({"techniques":[]})
        state = tech.process(self.root,max_items=10,daily_usd=tech.RESERVE_USD,
                             caller=fake,key="test")
        self.assertEqual(state["processed"],1)
        self.assertEqual(len(calls),1)
        self.assertEqual(state["stats"]["queue"]["pending"],1)
        self.assertEqual(state["stats"]["queue"]["done"],1)
        self.assertFalse((self.root / "work/scratch/codex-pass1/spore.sqlite3").exists())

    def test_propose_existing_technique_merges_only_evidence(self):
        fake,_ = self.fake_api()
        tech.process(self.root,max_items=1,caller=fake,key="test")
        first = tech.search(self.root,"EAX EDX custom register ABI",2)[0]
        original_problem = first["problem"]
        tech.memory.ingest(self.root, OTHER,1,"FAIL",note=EVIDENCE,
                           checker="NO MATCH size 500, retail 488")
        amended = self.proposal("extend", first["id"])
        amended["problem"] = "Other wording the model must not overwrite the existing problem"
        fake2,_ = self.fake_api({"techniques":[amended]})
        out = tech.process(self.root,max_items=1,caller=fake2,key="test")
        self.assertEqual(out["processed"],1)
        self.assertEqual(out["technique_links_added"],1)
        newer = tech.search(self.root,"EAX EDX register ABI",2)[0]
        self.assertEqual(newer["problem"],original_problem)
        self.assertEqual({x["va"] for x in newer["sources"]}, {VA,OTHER})
        self.assertEqual(tech.status(self.root)["techniques"],1)
        memory_hits = tech.memory.technique_query(self.root,"EAX EDX ABI",1)
        self.assertEqual(len(memory_hits),1)
        self.assertTrue(any("Compiled size 532 versus 499" in
                            link.get("observation","") for link in memory_hits[0]["sources"]))

    def test_unsupported_existing_id_and_category_are_rejected(self):
        with tech.memory.connection(self.root) as con:
            finding, evidence, known, _ = tech.make_prompt(con,1)
        for proposal in (
            self.proposal(action="extend",existing_id=999),
            {**self.proposal(),"category":"unrelated"}):
            with self.assertRaises(ValueError):
                tech._validate({"techniques":[proposal]},evidence,known)

    def test_semantic_candidates_are_offered_as_possible_merges(self):
        fake,_=self.fake_api()
        tech.process(self.root,caller=fake,key="test")
        prior=tech.search(self.root,"EAX register ABI",1)[0]["id"]
        tech.memory.ingest(self.root,OTHER,1,"FAIL",
                           note="This function has an unrelated generic description",
                           checker="NO MATCH")
        with mock.patch.object(tech.memory,"technique_query",
                               return_value=[{"id":prior}]) as finder:
            with tech.memory.connection(self.root) as con:
                finding,evidence,known,prompt=tech.make_prompt(con,2,repo=self.root)
        finder.assert_called_once()
        self.assertIn(prior,{row["id"] for row in known})
        self.assertIn("EXISTING TECHNIQUES",prompt)

    def test_source_version_change_requeues_and_invalidates_old_link(self):
        fake,_ = self.fake_api()
        tech.process(self.root,caller=fake,key="test")
        tech.memory.ingest(self.root, VA,1,"FAIL",
                           note=EVIDENCE+" New evidence: changed 3 bytes.",
                           checker="NO MATCH size 510, retail 499")
        with tech.memory.connection(self.root) as con:
            self.assertEqual(tech.synchronize(con),1)
        self.assertEqual(tech.status(self.root)["queue"]["pending"],1)
        self.assertEqual(tech.search(self.root,"EAX custom register ABI",4), [])

    def test_response_http_api_never_logs_key_and_pins_provider(self):
        payload = json.dumps({"choices":[{"message":{
            "content":json.dumps({"techniques":[]})}}],
            "usage":{"cost":0.001,"prompt_tokens":50,"completion_tokens":10}}).encode()
        calls = []
        def opener(req,timeout):
            calls.append((req,timeout))
            return io.BytesIO(payload)
        with mock.patch.object(tech.urllib.request,"urlopen",side_effect=opener):
            out,usage = tech.call_deepseek("TOP-SECRET-TEST-KEY","test input")
        req,_ = calls[0]
        sent = json.loads(req.data)
        self.assertEqual(sent["model"],tech.MODEL)
        self.assertEqual(sent["provider"]["only"],["sail-research"])
        self.assertFalse(sent["provider"]["allow_fallbacks"])
        self.assertEqual(sent["response_format"]["type"],"json_object")
        self.assertEqual(out,{"techniques":[]})
        self.assertEqual(usage["cost"],0.001)
        self.assertNotIn("TOP-SECRET-TEST-KEY",str(sent))

    def test_spend_ledger_records_actual_cost_not_full_reservation(self):
        fake,_ = self.fake_api()
        tech.process(self.root,caller=fake,key="test")
        with tech.memory.connection(self.root) as con:
            record = con.execute("SELECT reserved_usd,cost_usd,state FROM technique_spend").fetchone()
        self.assertEqual(record["reserved_usd"],tech.RESERVE_USD)
        self.assertAlmostEqual(record["cost_usd"],0.0009)
        self.assertEqual(record["state"],"complete")

    def test_technique_local_embedding_index(self):
        try:
            import sqlite_vec
        except ImportError:
            self.skipTest("sqlite-vec optional for stdlib-only tests")
        fake,_ = self.fake_api()
        tech.process(self.root,caller=fake,key="test")
        tech.memory.initialize(self.root,vectors=True)
        class FakeEmbedder:
            def encode(self,texts):
                return [[1.0]+[0.0]*(tech.memory.DIM-1) for _ in texts]
        service = tech.memory.Service.__new__(tech.memory.Service)
        service.repo = self.root
        service.folder = tech.memory.paths(self.root)[1]
        service.embedder = FakeEmbedder()
        self.assertEqual(service.index_techniques_once(),1)
        self.assertEqual(service.index_techniques_once(),0)
        found = service.search_techniques("EAX EDX custom calling convention",2)
        self.assertEqual(len(found),1)
        self.assertEqual(found[0]["sources"][0]["va"],VA)

    def test_repeated_verified_matches_do_not_verify_technique_causation(self):
        fake,_ = self.fake_api()
        tech.process(self.root,caller=fake,key="test")
        found = tech.search(self.root,"EAX register ABI",1)[0]
        self.assertNotEqual(found["evidence_level"],"verified")
        self.assertNotEqual(found["evidence_level"],"reproduced")

    def test_bad_missing_key_never_reserves_spend(self):
        with mock.patch.object(tech,"_get_key",side_effect=RuntimeError("no key")):
            with self.assertRaisesRegex(RuntimeError,"no key"):
                tech.process(self.root,max_items=1)
        self.assertEqual(tech.status(self.root)["reserved_or_spent_today_usd"],0)

    def test_concurrent_process_lock_refuses_second_writer(self):
        handle = tech._lock(self.root)
        try:
            with self.assertRaisesRegex(RuntimeError,"Another consolidator"):
                tech._lock(self.root)
        finally:
            handle.close()

    def test_shadow_dis_py_with_python314_friendly_cli(self):
        scratch = self.root / "work/scratch"
        scratch.mkdir(parents=True,exist_ok=True)
        other = SOURCE.parent
        for name in ("spore_technique_consolidator.py",
                     "spore_collective_memory.py","spore_memory_evidence.py"):
            (scratch/name).write_text((other/name).read_text())
        (scratch/"dis.py").write_text(
            'raise RuntimeError("shadowing standard library dis")\n')
        r=subprocess.run([sys.executable,str(scratch/SOURCE.name),
                          "--repo",str(self.root),"status"],
                          cwd=self.root,text=True,capture_output=True,timeout=12)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('"techniques": 0',r.stdout)

    def test_automatic_service_installation_is_explicit_and_shell_valid(self):
        helper = ROOT / "enable_automatic_consolidation.sh"
        self.assertTrue(helper.is_file())
        result = subprocess.run(["bash","-n",str(helper)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        source = helper.read_text()
        self.assertIn("systemctl --user enable --now",source)
        self.assertIn("nohup",source)
        self.assertIn("--disable",source)
        self.assertIn("--daily-usd",source)

    def test_installer_opt_in_and_runner_uses_technique_search(self):
        installer = (ROOT/"install_collective_memory.sh").read_text()
        setup = (ROOT/"spore_agentic.py").read_text()
        runner = (ROOT/"engine/spore_provider_fix/work/scratch/spore_first_pass_sqlite.py").read_text()
        self.assertIn("spore_technique_consolidator.py",installer)
        self.assertIn("spore_technique_consolidator.py",setup)
        self.assertIn("collective_memory.technique_query",runner)
        self.assertNotIn("spore_technique_consolidator.py --repo \"$REPO\" serve",
                         installer)


if __name__=="__main__":
    unittest.main()
