"""Attributed worker throughput for the existing SAD Agent cards."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import spore_dashboard_data as data


class WorkerThroughputTests(unittest.TestCase):
    def setUp(self):
        data._last_worker_rate.cache_clear()

    def test_distinct_worker_logs_show_distinct_output_rates(self):
        with tempfile.TemporaryDirectory() as d:
            a,b=(Path(d)/'a.jsonl',Path(d)/'b.jsonl')
            def save(file,span,generated):
                t=time.time()-5
                events=[
                    {"type":"step_start","timestamp":t},
                    {"type":"step_finish","timestamp":t+span,
                     "part":{"tokens":{"output":generated}}},
                ]
                file.write_text(''.join(json.dumps(x)+'\n' for x in events))
            save(a,2,40)
            save(b,4,40)
            self.assertEqual(data.worker_token_rate(a),20.0)
            self.assertEqual(data.worker_token_rate(b),10.0)

    def test_no_completed_step_never_claims_tps(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'agent.jsonl'
            p.write_text(json.dumps({"timestamp":time.time(),"type":"step_start"})+'\n')
            self.assertIsNone(data.worker_token_rate(p))

    def test_stale_steps_hidden(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'agent.jsonl'
            t=time.time()-500
            p.write_text(json.dumps({"type":"step_start","timestamp":t})+'\n'
                         +json.dumps({"type":"step_finish","timestamp":t+2,
                                       "part":{"tokens":{"output":200}}})+'\n')
            self.assertIsNone(data.worker_token_rate(p))

    def test_ms_timestamps_are_supported(self):
        now=int(time.time()*1000)
        self.assertAlmostEqual(data._event_seconds(now),now/1000,places=3)

    def test_no_cross_worker_slot_assignments_in_dashboard(self):
        source=(ROOT/'dashboard_web/src/views/Operations.tsx').read_text()
        self.assertIn('d?.workers?.map(w=>',source)
        self.assertIn('w.tps',source)
        self.assertNotIn('inference.slots.map',source)
        self.assertNotIn('Local inference slots',source)
        backend=(ROOT/'spore_dashboard_data.py').read_text()
        self.assertIn('worker_token_rate(logfile)',backend)
        self.assertNotIn('def local_inference(',backend)

    def test_no_extra_model_slot_http_endpoint(self):
        server=(ROOT/'dashboard/server.py').read_text()
        self.assertNotIn('/api/local-inference',server)


if __name__=='__main__':
    unittest.main()
