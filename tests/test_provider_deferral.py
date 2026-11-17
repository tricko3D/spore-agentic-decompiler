"""Provider failures rejoin the work-stealing queue after a backoff."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ENGINE=Path(__file__).resolve().parents[1]/'engine/spore_provider_fix/work/scratch'


class ProviderDeferralTests(unittest.TestCase):
    def test_delayed_function_is_stolen_when_ready(self):
        with tempfile.TemporaryDirectory() as t:
            code=r'''
import time
from unittest.mock import patch
import spore_sqlite_store as store
store.init_schema()
with store.connect() as db:
 db.executemany("INSERT INTO queue(va,position,size,suggested_file) VALUES(?,?,?,?)",
                [('0x11',1,20,'a.cpp'),('0x22',2,21,'b.cpp')])
store.create_run('run-1',2,3,{'0x11':0,'0x22':1})
store.defer_run_target('run-1','0x11',60)
va,_=store.claim_next('run-1',0,3)
assert va=='0x22',va
store.release_claim(va,'run-1',0)
# The delayed function remains queued and is claimed by another worker.
with patch('spore_sqlite_store.time.time',return_value=time.time()+90):
 va,_=store.claim_next('run-1',1,3)
assert va=='0x11',va
store.release_claim(va,'run-1',1)
'''
            env=os.environ.copy()
            env['PYTHONPATH']=str(ENGINE)
            p=subprocess.run([sys.executable,'-c',code],cwd=t,env=env,text=True,capture_output=True)
            self.assertEqual(p.returncode,0,p.stdout+'\n'+p.stderr)
