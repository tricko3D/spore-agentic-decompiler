"""No network: SQLite transaction, shared source locking, and crashed-worker ownership."""
from pathlib import Path
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ENGINE = Path(__file__).resolve().parents[1] / 'engine/spore_provider_fix/work/scratch'

class WorkStealingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root
        self.env = dict(os.environ, PYTHONPATH=str(ENGINE))

    def call(self, script):
        p = subprocess.run([sys.executable, '-c', script], cwd=self.root,
                           env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + '\n' + p.stderr)

    def test_claims_steal_without_concurrent_shared_source_edits(self):
        self.call('''
import spore_sqlite_store as st
st.init_schema()
with st.connect() as db:
    db.executemany('INSERT INTO queue(va,position,size,suggested_file) VALUES(?,?,?,?)',
        [(f'0x{i:08x}',i,20,f'file{i}.cpp') for i in (1,2,3)])
assignments={f'0x{i:08x}': i%2 for i in (1,2,3)}
st.create_run('r1',2,3,assignments,{'0x00000001':'same.cpp','0x00000002':'same.cpp','0x00000003':'different.cpp'})
a,busy=st.claim_next('r1',0,3)
assert a=='0x00000001' and busy
b,busy=st.claim_next('r1',1,3)
assert b=='0x00000003' and busy, b
c,busy=st.claim_next('r1',2,3)
assert c is None and busy
with st.connect() as db:
    db.execute("INSERT INTO attempts(va,pass_number,status,utc) VALUES(? ,1,'MATCH','now')",(a,))
st.release_claim(a,'r1',0)
d,busy=st.claim_next('r1',2,3)
assert d=='0x00000002',d
with st.connect() as db:
    db.execute("INSERT INTO attempts(va,pass_number,status,utc) VALUES(? ,1,'MATCH','now')",(b,))
st.release_claim(b,'r1',1)
st.ignore_run_target('r1', d, 'manifest')
st.release_claim(d,'r1',2)
e,busy=st.claim_next('r1',2,3)
assert e is None and not busy, (e,busy)
''')

    def test_dead_worker_reclaim_and_prior_void_passes(self):
        self.call('''
import os, subprocess, sys
import spore_sqlite_store as st
st.init_schema()
with st.connect() as db:
    db.execute("INSERT INTO queue(va,position,size,suggested_file) VALUES('0x00000001',1,20,'file.cpp')")
st.create_run('r1',2,3,{'0x00000001':0})
child=subprocess.run([sys.executable,'-c',"import spore_sqlite_store as st; print(st.claim_next('r1',0,3)[0])"],capture_output=True,text=True)
assert child.returncode==0 and child.stdout.strip()=='0x00000001', (child.stdout,child.stderr)
va,busy=st.claim_next('r1',1,3)
assert va=='0x00000001' and busy,(va,busy)
st.release_claim(va,'r1',1)
with st.connect() as db:
    for i in range(3):
        db.execute("INSERT INTO attempts(va,pass_number,status,utc) VALUES('0x00000001',?,'DEFER','now')",(i+1,))
assert st.claim_next('r1',1,3)==(None,False)
with st.connect() as db:
    db.execute("INSERT INTO attempt_reviews(attempt_id,classification,reason,evidence,reviewed_utc) SELECT id,'VOID_PROVIDER','test','test','now' FROM attempts")
assert st.claim_next('r1',1,3)[0]=='0x00000001'
''')

if __name__=='__main__':
    unittest.main()
