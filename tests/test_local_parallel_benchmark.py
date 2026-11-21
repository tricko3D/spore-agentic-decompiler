"""Offline confidence checks for the local Inkling throughput benchmark."""
import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
benchmark = importlib.import_module("benchmark_local_parallel")


class ParallelBenchmarkTests(unittest.TestCase):
    def test_candidate_parsing(self):
        self.assertEqual(benchmark.numbers("8,2,4,2,1"), [1, 2, 4, 8])
        self.assertEqual(benchmark.numbers("40,64"), [40,64])
        with self.assertRaises(Exception):
            benchmark.numbers("0,40")
        with self.assertRaises(Exception):
            benchmark.numbers("not-an-integer")

    def test_winner_maximizes_aggregate_not_worker_count(self):
        rows = [
            {"status": "ok", "workers": 2, "aggregate_output_tps": 48},
            {"status": "ok", "workers": 4, "aggregate_output_tps": 71},
            {"status": "ok", "workers": 8, "aggregate_output_tps": 68},
            {"status": "memory_pressure", "workers": 16, "aggregate_output_tps": 101},
        ]
        self.assertEqual(benchmark.best_profile(rows)["workers"], 4)

    def test_near_tie_preserves_resources(self):
        rows = [
            {"status": "ok", "workers": 4, "aggregate_output_tps": 97.5},
            {"status": "ok", "workers": 8, "aggregate_output_tps": 100},
        ]
        self.assertEqual(benchmark.best_profile(rows)["workers"], 4)

    def test_parallel_round_counts_real_completed_tokens(self):
        def fake_send(model, prompt, max_output, timeout, request_id):
            self.assertEqual(model, "local-test")
            self.assertGreater(len(prompt), 1024)
            return {"output_tokens": 240, "input_tokens": 600,
                    "latency_seconds": 0.1, "request_id": request_id}

        row = benchmark.round_concurrency("local-test", 4, 2, 256, 30,
                                          1000, send=fake_send)
        self.assertEqual(row["generated_tokens"], 4 * 240)
        self.assertEqual(row["prompt_tokens"], 4 * 600)
        self.assertEqual(len(row["requests"]), 4)
        self.assertGreater(row["elapsed_seconds"], 0)

    def test_save_profile_is_readable_by_launcher(self):
        with tempfile.TemporaryDirectory() as d:
            output = Path(d) / "measurements.json"
            profile = Path(d) / "profile.json"
            args = SimpleNamespace(per_slot_context=32768,
                                   candidates=[1, 2, 4],
                                   output_tokens=320,
                                   prompt_kib=8, rounds=2)
            rows = [{
                "status": "ok", "workers": 4, "threads": 8,
                "cpu_expert_layers": 10, "total_context": 4 * 32768,
                "slot_context": 32768, "aggregate_output_tps": 97.25,
                "mean_latency_seconds": 11.2,
            }]
            with patch.object(benchmark, "OUTPUT", output), patch.object(benchmark, "PROFILE", profile):
                winner = benchmark.save_results(rows, args, status="complete")
                self.assertEqual(winner["workers"], 4)
                self.assertTrue(output.is_file())
                selected = json.loads(profile.read_text())["recommended"]
                self.assertEqual(selected["workers"], 4)
                self.assertEqual(selected["threads"], 8)
                self.assertEqual(selected["slot_context"], 32768)
                self.assertEqual(selected["total_context"], 131072)

    def test_saved_model_identity_is_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            output = Path(d) / "measurements.json"
            profile = Path(d) / "profile.json"
            args = SimpleNamespace(per_slot_context=32768,
                                   candidates=[2, 4], output_tokens=320,
                                   prompt_kib=8, rounds=2)
            rows = [{"status": "ok", "workers": 4, "threads": 6,
                     "model_id": "Inkling-2-Mini-UD-Q4_K_M.gguf",
                     "cpu_expert_layers": 12, "slot_context": 32768,
                     "total_context": 131072, "aggregate_output_tps": 80,
                     "mean_latency_seconds": 10}]
            with patch.object(benchmark, "OUTPUT", output), patch.object(benchmark, "PROFILE", profile):
                benchmark.save_results(rows, args)
                self.assertEqual(json.loads(profile.read_text())["recommended"]["model_id"],
                                 "Inkling-2-Mini-UD-Q4_K_M.gguf")

    def test_no_valid_results_never_invents_profile(self):
        self.assertIsNone(benchmark.best_profile([{
            "status": "error", "workers": 16, "aggregate_output_tps": 0
        }]))


if __name__ == "__main__":
    unittest.main()
