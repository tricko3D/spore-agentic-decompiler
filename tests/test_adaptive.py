"""Pure local congestion control tests, no inference or payment."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ENGINE = Path(__file__).resolve().parents[1] / 'engine/spore_provider_fix/work/scratch'

class AdaptiveTests(unittest.TestCase):
    def test_aimd_probe_cooldown_atomic_slots_and_fixed_mode(self):
        with tempfile.TemporaryDirectory() as t:
            script = '''
import os, time
import spore_sqlite_store as store
import spore_adaptive as ctl
from unittest.mock import patch
store.init_schema()
ctl.begin('run-1', ceiling=8, start=1)
assert ctl.snapshot()['concurrency'] == 1
now = time.time()
assert ctl.observe('run-1', productive=True, now=now+95) is None
assert '1->2' in ctl.observe('run-1', productive=True, now=now+96)
assert ctl.snapshot()['concurrency'] == 2
with patch('spore_sqlite_store.time.time', return_value=now+99):
    a = store.reserve_inference_slot('0x11','run-1',8)
    b = store.reserve_inference_slot('0x22','run-1',8)
    c = store.reserve_inference_slot('0x33','run-1',8)
    assert a and b and c is None
store.release_inference_slot(a)
store.release_inference_slot(b)
assert '2->1' in ctl.observe('run-1', overloaded=True, now=now+100)
with patch('spore_sqlite_store.time.time', return_value=now+120):
    assert store.reserve_inference_slot('0x11','run-1',8) is None
with patch('spore_sqlite_store.time.time', return_value=now+161):
    a = store.reserve_inference_slot('0x11','run-1',8)
    b = store.reserve_inference_slot('0x22','run-1',8)
    assert a and b is None
store.release_inference_slot(a)
assert ctl.snapshot()['provider_failures'] == 1
assert ctl.snapshot()['concurrency'] == 1
assert ctl.observe('run-1', overloaded=True, now=now+121) is None
assert ctl.snapshot()['provider_failures'] == 2
assert ctl.snapshot()['consecutive_errors'] == 1
# Restarting a batch creates a fresh controller, no archived attempts deleted.
ctl.begin('run-2', ceiling=4)
assert ctl.snapshot()['run_id'] == 'run-2'
assert ctl.snapshot()['provider_failures'] == 0
assert ctl.observe('run-1', overloaded=True) is None
assert ctl.snapshot()['provider_failures'] == 0
os.environ['SPORE_ADAPTIVE_INFLIGHT']='0'
a = store.reserve_inference_slot('0x11','run-2',2)
b = store.reserve_inference_slot('0x22','run-2',2)
c = store.reserve_inference_slot('0x33','run-2',2)
assert a and b and c is None
'''
            env=os.environ.copy()
            env['PYTHONPATH']=str(ENGINE)
            env['SPORE_ADAPTIVE_INFLIGHT']='1'
            p=subprocess.run([sys.executable,'-c',script],cwd=t,env=env,
                            text=True,capture_output=True)
            self.assertEqual(p.returncode,0,p.stdout+'\n'+p.stderr)

if __name__ == '__main__':
    unittest.main()
