"""SAD Collective Memory integration tests, no model download or API calls."""
import importlib.util
import json
from pathlib import Path
import sqlite3
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
            def search(self, text, count):
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

    def test_runner_integration_and_independent_install_location(self):
        engine = (ROOT / "engine/spore_provider_fix/work/scratch/spore_first_pass_sqlite.py").read_text()
        setup = (ROOT / "spore_agentic.py").read_text()
        self.assertIn("collective_memory.ingest(", engine)
        self.assertIn("collective_memory.query(", engine)
        self.assertIn("spore_collective_memory.py", setup)
        self.assertIn("work/scratch/codex-pass1/memory", FILE.read_text())


if __name__ == "__main__":
    unittest.main()
