"""SAD Collective Memory integration tests, no model download or API calls."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
FILE = ROOT / "engine/spore_provider_fix/work/scratch/spore_collective_memory.py"
SPEC = importlib.util.spec_from_file_location("test_collective_memory_module", FILE)
mem = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mem)

VA = "0x00e61d90"
OTHER = "0x00672db0"


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.evidence = (self.root / "work/scratch/codex-pass1/investigations" / VA)
        self.evidence.mkdir(parents=True)
        (self.evidence / "keep.md").write_text(
            "EAX custom ABI; changing caller cleanup removes 4 mismatching bytes\n")
        (self.evidence / "source-diff.patch").write_text(
            "--- initial.cpp\n+++ latest.cpp\n@@ ...\n+ __fastcall\n")
        mem.initialize(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, va=VA, pass_number=1, status="FAIL", note="",
            checker="NO MATCH size=138/144 diffs=6"):
        return mem.ingest(
            self.root, va, pass_number, status, note=note, checker=checker,
            evidence_path=str(self.evidence.relative_to(self.root)))

    def test_separate_db_and_wal_mode(self):
        self.assertTrue(mem.paths(self.root)[2].is_file())
        db = mem.connection(self.root)
        try:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(db.execute("SELECT count(*) FROM findings").fetchone()[0], 0)
        finally:
            db.close()
        self.assertFalse((self.root / "work/scratch/codex-pass1/spore.sqlite3").exists())

    def test_missing_install_fails_open_for_runner(self):
        with tempfile.TemporaryDirectory() as raw:
            self.assertFalse(mem.ingest(Path(raw), VA, 1, "FAIL", note="x", checker="y"))
            self.assertEqual(mem.query(raw, "ABI mismatch"), [])

    def test_ingest_is_idempotent_and_indexes_evidence(self):
        self.assertTrue(self.add(note="EAX callee-save stack cleanup"))
        self.assertFalse(self.add(note="EAX callee-save stack cleanup"))
        db = mem.connection(self.root)
        try:
            self.assertEqual(db.execute("SELECT count(*) FROM findings").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM embedding_jobs").fetchone()[0], 1)
            data = db.execute("SELECT * FROM findings").fetchone()
            self.assertIn("fastcall", data["details"])
            self.assertIn("EAX", data["summary"])
        finally:
            db.close()

    def test_update_replaces_fts_row_and_pending_embedding(self):
        self.add(note="failed old attempt")
        self.assertTrue(self.add(note="EAX stack cleanup fixed"))
        db = mem.connection(self.root)
        try:
            self.assertEqual(db.execute("SELECT count(*) FROM findings_fts").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM embedding_jobs").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT summary FROM findings").fetchone()[0].count("fixed"), 1)
        finally:
            db.close()

    def test_full_text_fallback_prefers_relevant_compiler_findings(self):
        self.add(note="EAX calling convention and caller stack cleanup")
        mem.ingest(self.root, OTHER, 1, "FAIL", note="unrelated OpenGL texture issue",
                   checker="NO MATCH size mismatch")
        with mock.patch.object(mem, "request", return_value=None):
            hits = mem.query(self.root, "EAX calling convention stack cleanup", 2)
        self.assertEqual(hits[0]["va"], VA)
        self.assertIn("VERIFIED", mem.print_findings([{"verified_match": True,
                         "va": VA, "pass": 1, "summary": "Match", "evidence_path": "x"}]))

    def test_verified_flag_only_for_independently_checked_match(self):
        self.add(status="MATCH", checker="MATCH independently byte-exact")
        result = mem.query(self.root, "byte exact", 1)
        self.assertEqual(result[0]["verified_match"], True)
        self.assertEqual(result[0]["status"], "MATCH")

    def test_reject_bad_address_or_evidence_outside_checkout(self):
        with self.assertRaises(ValueError):
            mem.ingest(self.root, "0xGGG", 1, "FAIL", note="", checker="")
        with self.assertRaises(ValueError):
            mem.ingest(self.root, VA, 0, "FAIL", note="", checker="")
        with tempfile.TemporaryDirectory() as external:
            self.assertTrue(mem.ingest(self.root, VA, 1, "FAIL", note="safe",
                                       checker="NO MATCH", evidence_path=external))
        row = mem.connection(self.root)
        try:
            self.assertEqual(row.execute("SELECT evidence_path FROM findings").fetchone()[0], "")
        finally:
            row.close()

    def test_backfill_does_not_write_operational_sqlite_and_skips_unverified(self):
        original = self.root / "work/scratch/codex-pass1/spore.sqlite3"
        original.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(original)
        db.executescript("""
            CREATE TABLE attempts (
                id INTEGER PRIMARY KEY,va TEXT,pass_number INTEGER,status TEXT,note TEXT);
            CREATE TABLE attempt_reviews (attempt_id INTEGER PRIMARY KEY);
            INSERT INTO attempts VALUES
              (1,'0x00e61d90',1,'FAIL','Pass 1: EAX ABI | checker: NO MATCH size=138/144'),
              (2,'0x00672db0',2,'MATCH','Pass 2: independently byte-exact (MATCH)'),
              (3,'0x00e61d90',2,'MATCH','Unverified model claim'),
              (4,'0x00e61d90',3,'FAIL','Rejected historical claim');
            INSERT INTO attempt_reviews VALUES(4);
        """)
        db.close()
        before = original.read_bytes()
        result = mem.backfill(self.root)
        self.assertEqual(result["imported"], 2)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(original.read_bytes(), before)
        self.assertEqual(mem.backfill(self.root)["imported"], 0)
        hits = mem.query(self.root, "independently byte-exact", 5)
        self.assertTrue(any(hit["verified_match"] for hit in hits))

    def test_local_unix_socket_has_fallback_and_status(self):
        class Dummy:
            def search(self, text, count, verified_only=False):
                return [{"va": VA, "pass": 4, "verified_match": False,
                         "summary": text, "evidence_path": ""}]
        socket_path = mem.paths(self.root)[3]
        server = mem.UnixServer(str(socket_path), mem.Handler)
        server.service = Dummy()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            reply = mem.request(self.root, {"method": "status"})
            self.assertEqual(reply["vector_dim"], 384)
            self.assertEqual(mem.query(self.root, "EAX", 1)[0]["va"], VA)
        finally:
            server.shutdown()
            server.server_close()
            socket_path.unlink(missing_ok=True)
            thread.join(timeout=2)

    def test_vector_index_and_hybrid_query_without_real_model(self):
        try:
            import sqlite_vec  # noqa: F401
        except ImportError:
            self.skipTest("sqlite-vec not installed (optional for stdlib tests)")
        self.add(note="caller EAX ABI")
        mem.ingest(self.root, OTHER, 1, "FAIL", note="GL raster texture",
                   checker="NO MATCH graphics")
        class FakeEmbedder:
            def encode(self, texts):
                return [[1.0, 0.0] + [0.0] * (mem.DIM-2) if
                         "EAX" in x or "ABI" in x else
                         [0.0, 1.0] + [0.0] * (mem.DIM-2) for x in texts]
        svc = mem.Service.__new__(mem.Service)
        svc.repo = self.root
        svc.folder = mem.paths(self.root)[1]
        svc.embedder = FakeEmbedder()
        mem.initialize(self.root, vectors=True)
        self.assertEqual(svc.index_once(), 2)
        self.assertEqual(svc.index_once(), 0)
        self.assertEqual(svc.search("EAX ABI stack cleanup", 1)[0]["va"], VA)

    def test_script_entrypoint_ignores_shadowing_dis_module(self):
        """The upstream checkout's scratch/dis.py must not shadow stdlib dis."""
        scratch = self.root / "work/scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        script = scratch / "spore_collective_memory.py"
        script.write_text(FILE.read_text(encoding="utf-8"), encoding="utf-8")
        source_parser = FILE.with_name("spore_memory_evidence.py")
        (scratch / source_parser.name).write_text(
            source_parser.read_text(encoding="utf-8"), encoding="utf-8")
        (scratch / "dis.py").write_text(
            'raise RuntimeError("shadowed dis.py was imported")\n', encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(script), "--repo", str(self.root), "status"],
            cwd=self.root, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"installed": true', result.stdout)
        self.assertNotIn("shadowed dis.py", result.stderr)

    def _write_tool_log(self, va=VA):
        folder = self.root / "work/scratch/codex-pass1/logs"
        folder.mkdir(parents=True, exist_ok=True)
        log = folder / "checker.jsonl"
        events = [
            {"type": "tool_use", "part": {"state": {"status": "completed",
                "input": {"command": "sed -n '1,80p' tools/contrib/check.py"},
                "output": "CHECKER SOURCE"}}},
            {"type": "tool_use", "part": {"state": {"status": "completed",
                "input": {"command": f"cd /tmp; .venv/bin/python tools/contrib/check.py {va} --flags '/O2 /MD'"},
                "output": "NO MATCH 0x00e61d90 [O2]\nsize=141/144 diffs=12 offsets=0x01,0x02"}}},
            {"type": "tool_use", "part": {"state": {"status": "completed",
                "input": {"command": f".venv/bin/python tools/contrib/check.py {va} --flags '/O2 /MD /Gy'"},
                "output": "NO MATCH 0x00e61d90 [O2]\nsize=144/144 diffs=3 offsets=0x04,0x07"}}},
            {"type": "tool_use", "part": {"state": {"status": "running",
                "input": {"command": f".venv/bin/python tools/contrib/check.py {va}"},
                "output": "size=144/144 diffs=0"}}},
        ]
        log.write_text("\n".join(json.dumps(event) for event in events) + "\n")
        return log

    def test_duplicate_historical_summary_is_removed(self):
        self.assertTrue(self.add(
            note="register allocation differs; last pointer spills to stack.",
            checker="register allocation differs; last pointer spills to stack."))
        found = mem.query(self.root, "register allocation pointer", 1)
        self.assertEqual(found[0]["summary"],
                         "register allocation differs; last pointer spills to stack.")
        self.assertEqual(found[0]["summary"].count("register allocation"), 1)

    def test_historical_import_extracts_actual_checker_flags_and_metrics(self):
        log = self._write_tool_log()
        self.assertTrue(mem.ingest(self.root, VA, 1, "FAIL",
                                   note="register allocation mismatch",
                                   checker="size=144/144 diffs=3",
                                   log_path=str(log)))
        con = mem.connection(self.root)
        try:
            rows = con.execute("""SELECT ordinal,flags,size_compiled,
                size_retail,diff_bytes FROM finding_experiments
                ORDER BY ordinal""").fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual([row["diff_bytes"] for row in rows], [12, 3])
            self.assertEqual(rows[1]["flags"], "/O2 /MD /Gy")
        finally:
            con.close()
        hit = mem.query(self.root, "register allocation", 1)[0]
        self.assertEqual(len(hit["experiments"]), 2)
        self.assertIn("3 different bytes", hit["experiments"][-1])
        self.assertIn("Checks:", mem.print_findings([hit]))
        self.assertTrue(mem.evidence.experiment_brief(
            mem.evidence.parse_experiments(log, VA, self.root)).count("improved"))

    def test_backfill_replays_checker_log_without_touching_operational_db(self):
        log = self._write_tool_log()
        db_path = self.root / "work/scratch/codex-pass1/spore.sqlite3"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db_path)
        con.executescript("""CREATE TABLE attempts (
            id INTEGER PRIMARY KEY,va TEXT,pass_number INTEGER,status TEXT,
            note TEXT,log_path TEXT);
            CREATE TABLE attempt_reviews(attempt_id INTEGER);
        """)
        con.execute("INSERT INTO attempts VALUES(1,?,?,?,?,?)",
                    (VA, 1, "FAIL", "register allocation mismatched", str(log)))
        con.commit()
        con.close()
        before = db_path.read_bytes()
        res = mem.backfill(self.root)
        self.assertEqual(res["imported"], 1)
        self.assertEqual(db_path.read_bytes(), before)
        with mem.connection(self.root) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM finding_experiments").fetchone()[0], 2)

    def test_verified_only_does_not_invent_relevant_success(self):
        self.add(note="EAX caller register allocation")
        mem.ingest(self.root, OTHER, 1, "MATCH", note="Texture atlas sampler",
                   checker="independently byte-exact")
        self.assertEqual(mem.query(self.root, "EAX caller", 4,
                                   verified_only=True), [])
        self.assertEqual(mem.query(self.root, "Texture atlas", 4,
                                   verified_only=True)[0]["status"], "MATCH")

    def test_pinned_fastembed_api_and_model_registry(self):
        try:
            from fastembed import TextEmbedding
        except ImportError:
            self.skipTest("FastEmbed is optional in stdlib-only tests")
        import inspect
        parameters = inspect.signature(TextEmbedding).parameters
        self.assertIn("threads", parameters)
        self.assertIn("cuda", parameters)
        supported = TextEmbedding.list_supported_models()
        found = [row for row in supported if row.get("model") == mem.MODEL]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].get("dim"), mem.DIM)

    def test_verified_match_retrieves_real_local_cpp_not_made_up_advice(self):
        source = (self.root / "match/slices/s00e61d90/s00e61d90.cpp")
        source.parent.mkdir(parents=True)
        source.write_text("// @ 0x00e61d90\n__fastcall int f(int a) { return a + 1; }\n")
        self.assertTrue(self.add(status="MATCH", note="independently byte-exact",
                                 checker="MATCH"))
        con = mem.connection(self.root)
        try:
            text = con.execute("SELECT details FROM findings").fetchone()[0]
            self.assertIn("__fastcall", text)
            self.assertIn("Locally verified source excerpt", text)
        finally:
            con.close()
        self.assertTrue(mem.query(self.root, "fastcall", 2, verified_only=True))

    def test_unverified_experiment_never_claims_verified_cpp(self):
        source = self.root / "match/slices/s00e61d90/s00e61d90.cpp"
        source.parent.mkdir(parents=True)
        source.write_text("// @ 0x00e61d90\nint unreliable() { return 0; }\n")
        self.add(note="something failed")
        with mem.connection(self.root) as con:
            details = con.execute("SELECT details FROM findings").fetchone()[0]
        self.assertNotIn("unreliable", details)

    def test_stale_socket_protocol_falls_back_to_fresh_sqlite(self):
        self.add(note="EAX calling convention")
        with mock.patch.object(mem, "request", return_value={
            "results": [{"va": OTHER, "summary": "stale output"}]}):
            got = mem.query(self.root, "EAX calling", 1)
        self.assertEqual(got[0]["va"], VA)

    def test_runner_integration_and_independent_install_location(self):
        engine = (ROOT / "engine/spore_provider_fix/work/scratch/spore_first_pass_sqlite.py").read_text()
        setup = (ROOT / "spore_agentic.py").read_text()
        self.assertIn("collective_memory.ingest(", engine)
        self.assertIn("collective_memory.query(", engine)
        self.assertIn("spore_collective_memory.py", setup)
        self.assertIn("work/scratch/codex-pass1/memory", FILE.read_text())


if __name__ == "__main__":
    unittest.main()
