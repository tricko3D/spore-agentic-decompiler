"""Live per-slot llama.cpp dashboard regression tests, all offline."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import local_model_dashboard as monitor
import benchmark_local_parallel as benchmark


def slot(index, decoded, task=1, active=True, ctx=32768):
    return {
        "id": index, "id_task": task, "is_processing": active,
        "n_ctx": ctx, "next_token": {"n_decoded": decoded},
    }


class LocalModelDashboardTests(unittest.TestCase):
    def test_per_slot_and_aggregate_decoded_tokens_per_second(self):
        tracker = monitor.SlotRateTracker(window_seconds=3.0)
        baseline = tracker.sample([slot(0, 0, task=14), slot(1, 0, task=15)], now=10.0)
        self.assertEqual([x["rate"] for x in baseline], [0, 0])
        values = tracker.sample([slot(0, 12, task=14), slot(1, 24, task=15)], now=11.0)
        self.assertEqual([x["rate"] for x in values], [12, 24])
        text = monitor.render_dashboard(values, 120, "BENCHMARK MEASURING")
        self.assertIn("AGGREGATE  36.0 tok/s", text)
        self.assertIn("12.0 tok/s", text)
        self.assertIn("24.0 tok/s", text)
        self.assertIn("#01", text)
        self.assertIn("#02", text)

    def test_rollover_or_task_change_never_adds_phantom_tokens(self):
        tracker = monitor.SlotRateTracker()
        tracker.sample([slot(0, 100, task=1)], now=100.0)
        self.assertEqual(tracker.sample([slot(0, 120, task=1)], now=101.0)[0]["rate"], 20)
        self.assertEqual(tracker.sample([slot(0, 22, task=2)], now=102.0)[0]["rate"], 0)
        self.assertEqual(tracker.sample([slot(0, 30, task=2)], now=103.0)[0]["rate"], 8)

    def test_idle_resets_speed_and_stale_task_history(self):
        tracker = monitor.SlotRateTracker()
        tracker.sample([slot(0, 0)], now=0.0)
        tracker.sample([slot(0, 10)], now=1.0)
        idle = tracker.sample([slot(0, 10, active=False)], now=2.0)
        self.assertEqual(idle[0]["rate"], 0)
        self.assertEqual(idle[0]["status"], "IDLE")
        tracker.sample([slot(0, 100, task=1)], now=3.0)
        self.assertEqual(tracker.sample([slot(0, 107, task=1)], now=4.0)[0]["rate"], 7)

    def test_prefill_without_decodes_is_not_generating(self):
        tracker = monitor.SlotRateTracker()
        self.assertEqual(tracker.sample([slot(0, 0)], now=1)[0]["status"], "PREFILL")
        self.assertEqual(tracker.sample([slot(0, 0)], now=5)[0]["rate"], 0)

    def test_32_slot_grid_has_one_cell_per_slot(self):
        rows = [{"id": i, "status": "GEN", "rate": 1.25,
                 "decoded": 500, "context": 32768}
                for i in range(32)]
        lines = monitor.grid(rows, width=160)
        screen = "\n".join(lines)
        for i in range(32):
            self.assertIn(f"#{i+1:02d}", screen)
        self.assertEqual(len(lines), 8)
        self.assertIn("tok/s", screen)

    def test_read_only_dashboard_does_not_require_benchmark_active(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(monitor, "STATE_FILE", Path(d) / "no-live.json"), \
                 mock.patch.object(monitor, "RESULTS_FILE", Path(d) / "no-result.json"):
                stage, best = monitor.benchmark_details()
        self.assertIsNone(best)
        self.assertIn("Model server live", stage)

    def test_atomic_benchmark_stage_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "live.json"
            with mock.patch.object(benchmark, "LIVE", path):
                benchmark.publish_live("measuring", workers=8, round=1)
            state = json.loads(path.read_text())
            self.assertEqual(state["stage"], "measuring")
            self.assertEqual(state["workers"], 8)
            self.assertEqual(state["round"], 1)
            self.assertIn("updated_epoch", state)


if __name__ == "__main__":
    unittest.main()
