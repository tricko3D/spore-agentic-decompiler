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
    scope = {"json": json}
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
