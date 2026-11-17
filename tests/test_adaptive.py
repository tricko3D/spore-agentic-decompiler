"""Offline progressive output-token congestion control tests."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ENGINE=Path(__file__).resolve().parents[1]/'engine/spore_provider_fix/work/scratch'


class AdaptiveTests(unittest.TestCase):
    def test_token_verified_ramp_and_selective_rollback(self):
        with tempfile.TemporaryDirectory() as t:
            script=r'''
import os,time
from unittest.mock import patch
import spore_sqlite_store as store
import spore_adaptive as ctl
store.init_schema()
ctl.begin('run-1',ceiling=8,start=1)
a=store.reserve_inference_slot('0x11','run-1',8)
assert a and not store.reserve_inference_slot('0x22','run-1',8)
assert '1->2' in ctl.confirm_output('run-1',a)
assert ctl.confirm_output('run-1',a) is None
b=store.reserve_inference_slot('0x22','run-1',8)
assert b and not store.reserve_inference_slot('0x33','run-1',8)
assert '2->3' in ctl.confirm_output('run-1',b)
c=store.reserve_inference_slot('0x33','run-1',8)
assert '3->4' in ctl.confirm_output('run-1',c)
d=store.reserve_inference_slot('0x44','run-1',8)
assert d and not store.reserve_inference_slot('0x55','run-1',8)
store.release_inference_slot(d)
assert '4->3' in ctl.observe('run-1',overloaded=True)
assert ctl.snapshot()['productive_samples']==3
assert ctl.snapshot()['provider_failures']==1
with store.connect() as db:
    assert db.execute('SELECT COUNT(*) FROM inference_slots').fetchone()[0]==3
    assert db.execute('SELECT SUM(verified) FROM inference_slots').fetchone()[0]==3
assert not store.reserve_inference_slot('0x55','run-1',8)
future=time.time()+90
with patch('spore_sqlite_store.time.time',return_value=future):
    d=store.reserve_inference_slot('0x55','run-1',8)
assert d
# One new unique stream confirms capacity recovered.
assert '4->5' in ctl.confirm_output('run-1',d,now=future)
ctl.begin('run-2',ceiling=4,start=1)
assert ctl.confirm_output('run-1',a) is None
assert ctl.observe('run-1',overloaded=True) is None
'''
            env=os.environ.copy()
            env['PYTHONPATH']=str(ENGINE)
            env['SPORE_ADAPTIVE_INFLIGHT']='1'
            p=subprocess.run([sys.executable,'-c',script],cwd=t,env=env,text=True,capture_output=True)
            self.assertEqual(p.returncode,0,p.stdout+'\n'+p.stderr)
