"""Read-only /slots telemetry calculations for the existing SAD web UI."""
import json
from pathlib import Path
import sys
from unittest import TestCase, mock
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import spore_dashboard_data as data


class Reply:
    def __init__(self, value):
        self.payload = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.payload


def slot(index, count, task=3, active=True):
    return {"id":index, "id_task":task, "is_processing":active,
            "n_ctx":32768, "next_token":{"n_decoded":count}}


class LiveInferenceTests(TestCase):
    def setUp(self):
        data._LOCAL_INFERENCE_PREVIOUS.clear()

    def test_live_rates_and_aggregate_use_actual_decoded_token_deltas(self):
        with mock.patch.object(data, "urlopen", side_effect=[
            Reply([slot(0, 0), slot(1, 0)]),
            Reply([slot(0, 10), slot(1, 25)]),
        ]), mock.patch.object(data.time, "monotonic", side_effect=[100.0,101.0]):
            first = data.local_inference()
            second = data.local_inference()
        self.assertEqual(first["aggregate_tps"], 0.0)
        self.assertEqual(second["aggregate_tps"], 35.0)
        self.assertEqual([s["tps"] for s in second["slots"]], [10.0,25.0])
        self.assertEqual(len(second["slots"]),2)

    def test_changed_tasks_are_not_counted_across_requests(self):
        with mock.patch.object(data, "urlopen", side_effect=[
            Reply([slot(0, 100, task=1)]),
            Reply([slot(0, 121, task=1)]),
            Reply([slot(0, 9, task=2)]),
        ]), mock.patch.object(data.time, "monotonic", side_effect=[100.,101.,102.]):
            self.assertEqual(data.local_inference()["slots"][0]["tps"],0.0)
            self.assertEqual(data.local_inference()["slots"][0]["tps"],21.0)
            self.assertEqual(data.local_inference()["slots"][0]["tps"],0.0)

    def test_unavailable_server_has_no_invented_speed(self):
        with mock.patch.object(data,"urlopen",side_effect=urllib.error.URLError("offline")):
            result = data.local_inference()
        self.assertFalse(result["available"])
        self.assertIsNone(result["aggregate_tps"])
        self.assertEqual(result["slots"],[])

    def test_unknown_decode_counter_yields_null_tps(self):
        row = {"id":0,"is_processing":True,"id_task":1,"n_ctx":32768}
        with mock.patch.object(data, "urlopen", return_value=Reply([row])):
            result = data.local_inference()
        self.assertIsNone(result["aggregate_tps"])
        self.assertIsNone(result["slots"][0]["tps"])

    def test_backend_does_not_associate_server_slot_with_worker_id(self):
        source = (ROOT/"dashboard_web/src/views/Operations.tsx").read_text()
        self.assertIn("inference.slots.map(slot=>",source)
        self.assertIn("d?.workers?.map(w=>",source)

    def test_existing_dashboard_route_is_read_only(self):
        source = (ROOT/"dashboard/server.py").read_text()
        self.assertIn('if route=="/api/local-inference":',source)
        self.assertIn("return self.respond(local_inference())",source)
