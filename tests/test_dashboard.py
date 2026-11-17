"""Local-only HTTP status, no raw API keys or model transcripts."""
from pathlib import Path
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
import unittest

ROOT=Path(__file__).resolve().parents[1]
class DashboardTests(unittest.TestCase):
    def test_snapshot_and_local_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)
            work=repo/'work/scratch/codex-pass1'
            work.mkdir(parents=True)
            script='''import spore_sqlite_store as s
s.init_schema()
with s.connect() as db:
    db.execute("INSERT INTO queue(va,position,size,suggested_file) VALUES('0x00000001',1,20,'a.cpp')")
    db.execute("INSERT INTO attempts(va,pass_number,status,utc) VALUES('0x00000001',1,'MATCH','2026-11-17T20:00:00Z')")
s.create_run('r1',1,3,{'0x00000001':0})
'''
            env=os.environ.copy()
            env['PYTHONPATH']=str(ROOT/'engine/spore_provider_fix/work/scratch')
            setup=subprocess.run([sys.executable,'-c',script],env=env,cwd=repo,capture_output=True,text=True)
            self.assertEqual(setup.returncode,0,setup.stderr)
            sys.path.insert(0,str(ROOT))
            import spore_dashboard as dash
            snap=dash.snapshot(repo)
            self.assertEqual(snap['counters']['matches'],1)
            self.assertEqual(snap['run']['completed'],1)
            self.assertEqual(snap['events'][0]['kind'],'MATCH')
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0))
                port=sock.getsockname()[1]
            proc=subprocess.Popen([sys.executable,'-u','-c',
                'import spore_dashboard as dash; from pathlib import Path; dash.serve(Path(%r),%d)'%(str(repo),port)],
                env=dict(env,PYTHONPATH=str(ROOT)),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                for _ in range(35):
                    try:
                        with urlopen(f'http://127.0.0.1:{port}/api/snapshot',timeout=.5) as resp:
                            data=json.loads(resp.read())
                        break
                    except OSError:
                        time.sleep(.1)
                else:
                    self.fail('Dashboard server never started')
                self.assertEqual(data['counters']['matches'],1)
                with urlopen(f'http://127.0.0.1:{port}/',timeout=1) as resp:
                    html=resp.read().decode()
                self.assertIn('Recent events',html)
                self.assertNotIn('OPENROUTER_API_KEY',html)
            finally:
                proc.terminate()
                try:proc.wait(timeout=2)
                except subprocess.TimeoutExpired:proc.kill()
                if proc.stdout:proc.stdout.close()
                if proc.stderr:proc.stderr.close()

if __name__=='__main__':unittest.main()
