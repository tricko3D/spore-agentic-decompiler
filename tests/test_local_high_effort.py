"""Offline regression tests for local high-effort runner and recovery."""
import ast
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "engine/spore_provider_fix/work/scratch/spore_first_pass_sqlite.py"
PROMPT = ROOT / "engine/spore_local_high_effort_prompt.txt"
LAUNCHER = ROOT / "scripts/run_local_high_effort.py"


def extract(*functions):
    tree = ast.parse(RUNNER.read_text())
    selected = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in functions]
    if len(selected) != len(functions):
        raise AssertionError("Runner function missing")
    import os
    scope = {"json": json, "os": os, "Path": Path}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(RUNNER), "exec"), scope)
    return scope


class HighEffortTests(unittest.TestCase):
    def test_prompt_allows_iteration_and_enforces_independent_checker(self):
        prompt = PROMPT.read_text()
        self.assertIn("{CHECK_BUDGET}", prompt)
        self.assertIn("tools/contrib/check.py {VA}", prompt)
        self.assertIn("byte-exact", prompt)
        self.assertNotIn("run tools/contrib/check.py {VA} ONCE", prompt)
        self.assertIn("spore_local_high_effort_prompt.txt", RUNNER.read_text())

    def test_compaction_error_extracted_instead_of_invisible(self):
        fn = extract("parse_opencode_log", "local_compaction_failure")
        events = [
            {"type": "tool_use", "part": {"tool": "shell"}},
            {"type": "error", "error": {
                "type": "compaction.failed",
                "message": "Compaction summary reached the output token limit"
            }},
        ]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session.jsonl"
            path.write_text("\n".join(json.dumps(e) for e in events))
            _, problems, _ = fn["parse_opencode_log"](path)
        self.assertTrue(fn["local_compaction_failure"](problems))
        self.assertIn("compaction.failed", problems[0])
        self.assertFalse(fn["local_compaction_failure"](["model not found"]))

    def test_recovery_does_not_consume_attempt(self):
        source = RUNNER.read_text()
        self.assertIn("kind == 'COMPACTION'", source)
        self.assertIn("fresh_session=local_compaction_restarts > 0", source)
        self.assertIn("store.ignore_run_target(rid, va, 'local compaction recovery exhausted')", source)
        self.assertIn("if kind in ('IDLE', 'COMPACTION'):", source)
        self.assertIn("local_compaction_restarts += 1", source)

    def test_tool_budget_and_proof_hooks(self):
        scope = extract("local_tool_budget", "count_completed_tools")
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"SPORE_LOCAL_MAX_TOOLS": "30"}):
            self.assertEqual(scope["local_tool_budget"](), 30)
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "events.jsonl"
            events = [
                {"type": "tool_use", "part": {"callID": "a"}},
                {"type": "tool_use", "part": {"callID": "a"}},
                {"type": "tool_use", "part": {"callID": "b"}},
            ]
            log.write_text("\n".join(json.dumps(e) for e in events))
            self.assertEqual(scope["count_completed_tools"](log), 2)
        source = RUNNER.read_text()
        self.assertIn("checked_equivalence(va, seconds=proof_seconds)", source)
        self.assertIn("('EQUIV', '',", source)
        self.assertIn("remaining_tool_calls=", source)

    def test_parallel_24_hour_run_settings(self):
        source = LAUNCHER.read_text()
        self.assertIn('default="auto"', source)
        self.assertIn('selected["workers"]', source)
        self.assertIn('scripts/benchmark_local_parallel.py', source)
        self.assertIn('default=30,', source)
        self.assertIn('default=24,', source)
        self.assertIn('query_slots(base)', source)
        self.assertIn('"SPORE_ALL_QUEUED": "1"', source)
        self.assertIn('"--max-inflight", str(args.workers)', source)
        self.assertIn('"--all-modules"', source)
        self.assertIn('report_local_experiment.py', source)
        engine = RUNNER.read_text()
        self.assertIn("local_deadline_reached()", engine)
        self.assertIn("return 'DEADLINE'", engine)

    def test_equivalent_guard(self):
        engine = RUNNER.read_text()
        coordinator = (ROOT / 'engine/spore_provider_fix/spore_parallel_sqlite.py').read_text()
        self.assertIn('class EquivalentIndex:', engine)
        self.assertIn('or va in equivalents.refresh()', engine)
        self.assertIn('mod.EquivalentIndex().refresh()', coordinator)

    def test_equivalent_terminal_in_store(self):
        source = (ROOT / 'engine/spore_provider_fix/work/scratch/spore_sqlite_store.py').read_text()
        self.assertIn("'MATCH','FAIL','DEFER','SKIP','EQUIV'", source)
        self.assertIn("('MATCH','SKIP','EQUIV')", source)

    def test_offload_strategy_keeps_existing_baseline(self):
        tree = ast.parse(LAUNCHER.read_text())
        scope = {"__name__": "sad_local_test", "__file__": str(LAUNCHER)}
        exec(compile(tree, str(LAUNCHER), "exec"), scope)
        self.assertEqual(scope["expert_override"](12), scope["OVERRIDE"])
        self.assertEqual(scope["expert_override"](10).count("|"), 9)
        with self.assertRaises(ValueError):
            scope["expert_override"](3)

    def test_context_fallback_order_and_offload(self):
        tree = ast.parse(LAUNCHER.read_text())
        needed = {"query_server", "contexts", "cached_model", "start_server", "main"}
        self.assertTrue(needed.issubset({x.name for x in tree.body
                                        if isinstance(x, ast.FunctionDef)}))
        scope = {"__name__": "sad_local_test", "__file__": str(LAUNCHER)}
        exec(compile(tree, str(LAUNCHER), "exec"), scope)
        self.assertEqual(scope["contexts"](131072), [131072, 65536, 32768, 16384])
        self.assertEqual(scope["contexts"](65536), [65536, 32768, 16384])
        self.assertIn("ffn_.*_exps", scope["OVERRIDE"])
        self.assertIn("--fit", LAUNCHER.read_text())


if __name__ == "__main__":
    unittest.main()
