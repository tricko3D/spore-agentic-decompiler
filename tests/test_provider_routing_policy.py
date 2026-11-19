"""Offline verification of the production OpenCode DeepSeek routing policy."""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "engine/spore_provider_fix/work/scratch/spore_first_pass_sqlite.py"
EXPECTED = ["nebius/fp4", "relace/fp4", "stepfun/fp8"]


class ProviderRoutingPolicyTests(unittest.TestCase):
    def test_production_runner_uses_ordered_allowlist_and_price_ceiling(self):
        source = ast.parse(RUNNER.read_text(encoding="utf-8"))
        fn = next(node for node in source.body
                  if isinstance(node, ast.FunctionDef)
                  and node.name == "openrouter_config")
        module = ast.Module(body=[fn], type_ignores=[])
        namespace = {"OPENROUTER_MODEL": "deepseek/deepseek-v4.1-flash-1017"}
        exec(compile(ast.fix_missing_locations(module), str(RUNNER), "exec"),
             namespace)
        config = namespace["openrouter_config"]()
        model = namespace["OPENROUTER_MODEL"]
        payload = config["providers"]["openrouter"]["models"][model]["body"]
        self.assertEqual(config["model"], "openrouter/" + model)
        self.assertEqual(payload["provider"]["order"], EXPECTED)
        self.assertEqual(payload["provider"]["only"], EXPECTED)
        self.assertFalse(payload["provider"]["allow_fallbacks"])
        self.assertEqual(payload["provider"]["data_collection"], "deny")
        self.assertEqual(payload["provider"]["max_price"],
                         {"prompt": 0.18, "completion": 0.72})


if __name__ == "__main__":
    unittest.main()
