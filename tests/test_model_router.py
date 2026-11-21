"""Offline tests: cost ranking, model pinning, cache fallback and DeepInfra config."""
from __future__ import annotations
from contextlib import contextmanager
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "engine/spore_provider_fix/work/scratch"))
import spore_model_router as router


def entry(name, price="0.0000001", output="0.0000004", *, tools=True,
          context=131072, overrides=False):
    return {
        "id": name,
        "name": name,
        "architecture": {
            "input_modalities": ["text"], "output_modalities": ["text"]
        },
        "supported_parameters": ["tools"] if tools else ["temperature"],
        "context_length": context,
        "top_provider": {"max_completion_tokens": 16384},
        "pricing": {
            "prompt": price, "completion": output,
            "input_cache_read": "0.00000001",
            "input_cache_write": "0.00000015",
            "request": "0",
            "overrides": [{"condition": "time"}] if overrides else [],
        },
    }


class ModelRouterTests(unittest.TestCase):
    def test_manual_local_settings_accept_explicit_launch_environment(self):
        cfg={'SPORE_LOCAL_BASE_URL':'http://127.0.0.1:8080/v1',
             'SPORE_LOCAL_MODEL':'running-model',
             'SPORE_LOCAL_CONTEXT':'32768'}
        self.assertEqual(router.local_endpoint(cfg),cfg['SPORE_LOCAL_BASE_URL'])
        self.assertEqual(router.local_settings(cfg),(cfg['SPORE_LOCAL_BASE_URL'],'running-model'))
        self.assertEqual(router.local_limits(cfg),(32768,8192))
        with self.assertRaises(ValueError):
            router.local_endpoint({'SPORE_LOCAL_BASE_URL':'http://example.org/v1'})


    def test_local_worker_seed_is_stable_and_sent_in_model_body(self):
        environment={"SPORE_SQLITE_RUN_ID":"test-run-123",
                     "SPORE_SQLITE_WORKER_IDX":"8",
                     "SPORE_LOCAL_MODEL":"local-model"}
        with mock.patch.dict(router.os.environ,environment):
            config=router.local_opencode_config()
        body=config["providers"]["spore_local"]["models"]["inkling"]["body"]
        self.assertEqual(body["seed"],router.local_slot_seed("test-run-123",8))
        self.assertNotEqual(body["seed"],router.local_slot_seed("test-run-123",9))
        self.assertEqual(body["reasoning_effort"],"high")

    def test_price_weighting_and_cache(self):
        p = {
            "prompt": "0.000001", "completion": "0.000002",
            "input_cache_read": "0.0000001", "request": "0.00001",
        }
        cost = router.estimate(p, input_tokens=1000, output_tokens=100,
                               cached_fraction=0.5)
        self.assertAlmostEqual(cost, 0.0005 + 0.00005 + 0.0002 + 0.00001)

    def test_filters_tooling_context_conditional_prices_and_free(self):
        data = {"data": [
            entry("example/valid"),
            entry("example/no-tools", tools=False),
            entry("example/short", context=4096),
            entry("example/conditional", overrides=True),
            entry("example/free:free"),
            entry("example/negative", price="-1"),
        ]}
        matches = router.parse_catalog(data)
        self.assertEqual([c["model"] for c in matches], ["example/valid"])

    def test_cheapest_pins_for_same_pass_and_logs_match_quality(self):
        cheap = router.parse_catalog({"data": [entry("example/a"), entry("example/b", price="0.0000005")]})
        with sqlite3.connect(":memory:") as db, tempfile.TemporaryDirectory() as d:
            with mock.patch.object(router, "top_ten", return_value=cheap):
                first = router.choose(db, run_id="run1", va="0x00c00000",
                                      pass_number=1, policy="cheapest",
                                      cache_path=Path(d) / "catalog.json")
            with mock.patch.object(router, "top_ten", side_effect=AssertionError("should not refetch")):
                same = router.choose(db, run_id="run1", va="0x00c00000",
                                     pass_number=1, policy="cheapest",
                                     cache_path=Path(d) / "catalog.json")
            self.assertEqual(first, same)
            self.assertEqual(first["model"], "example/a")
            log = Path(d) / "attempt.jsonl"
            log.write_text(json.dumps({
                "type": "step_finish",
                "part": {"cost": 0.012, "tokens": {
                    "input": 300, "output": 100, "cache": {"read": 50}
                }},
            }) + "\n", encoding="utf-8")
            router.record(db, run_id="run1", va="0x00c00000",
                          pass_number=1, status="MATCH", log=log)
            self.assertEqual(router.performance(db, "openrouter", "example/a"), (1, 1))
            cost = db.execute("SELECT observed_cost FROM model_passes").fetchone()[0]
            self.assertAlmostEqual(cost, 0.012)

    def test_deepinfra_is_fixed_and_never_reuses_openrouter_choice(self):
        with sqlite3.connect(":memory:") as db:
            selected = router.choose(db, run_id="run1", va="0x00c00000", pass_number=1,
                                     provider="deepinfra", policy="fixed")
            self.assertEqual(selected["model"], router.DEEPINFRA_MODEL)
            with self.assertRaises(RuntimeError):
                router.choose(db, run_id="run1", va="0x00c00000", pass_number=1,
                              provider="openrouter", policy="fixed")
            with self.assertRaises(ValueError):
                router.choose(db, run_id="run1", va="0x00c00000", pass_number=2,
                              provider="deepinfra", policy="cheapest")
            config = router.direct_opencode_config(router.DEEPINFRA_MODEL, "medium")
            self.assertEqual(config["providers"]["spore_deepinfra"]["settings"]["baseURL"],
                             "https://api.deepinfra.com/v1/openai")
            self.assertEqual(config["providers"]["spore_deepinfra"]["env"],
                             ["DEEPINFRA_API_KEY"])

    def test_catalog_uses_recent_cache_without_network(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "catalog.json"
            data = {"data": [entry("example/valid")]}
            path.write_text(json.dumps(data))
            with mock.patch.object(router, "urlopen", side_effect=AssertionError("network fetch")):
                self.assertEqual(router.catalogue(path), data)

    def test_deepinfra_billing_requires_numeric_usage(self):
        with mock.patch.object(router, "urlopen", return_value=io.BytesIO(json.dumps({
            "months": [{"total_cost": 1.25}]
        }).encode())):
            self.assertAlmostEqual(router.deepinfra_month_spend("test-key"), 1.25)
        with mock.patch.object(router, "urlopen", return_value=io.BytesIO(json.dumps({
            "months": [{"total_cost": "unknown"}]
        }).encode())):
            with self.assertRaises(RuntimeError):
                router.deepinfra_month_spend("test-key")


if __name__ == "__main__":
    unittest.main()
