"""Offline results accounting, live agent stream and control-safety regression tests."""
import json
from pathlib import Path
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from spore_dashboard_data import snapshot,functions,function_detail,stream,log_cost,agent_detail
from spore_dashboard_control import Controller,modules


class ExtendedDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo=Path(self.temp.name)
        self.work=self.repo/'work/scratch/codex-pass1'
        self.work.mkdir(parents=True)
        env=dict(os.environ,PYTHONPATH=str(ROOT/'engine/spore_provider_fix/work/scratch'))
        code='''import spore_sqlite_store as s
s.init_schema()
with s.connect() as db:
 db.executemany('INSERT INTO queue(va,position,size,suggested_file) VALUES(?,?,?,?)',[('0x00000001',1,20,'src/game/SPGonzago/a.cpp'),('0x00000002',2,31,'src/game/SPGonzago/b.cpp'),('0x00000003',3,17,'src/game/SPGonzago/c.cpp')])
s.create_run('run1',2,3,{'0x00000001':0,'0x00000002':1,'0x00000003':0})
'''
        p=subprocess.run([sys.executable,'-c',code],cwd=self.repo,env=env,capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr)
        (self.repo/'src/game/SPGonzago').mkdir(parents=True)

    def test_outcomes_cost_effort_and_checker_evidence(self):
        logs=self.work/'logs';logs.mkdir()
        log=logs/'00000001.deepseek-openrouter.jsonl'
        lines=[
          {'type':'step_start','part':{'id':'step1'}},
          {'type':'step_finish','part':{'cost':0.005,'tokens':{'output':110}}},
          {'type':'text','part':{'text':'Working on code'}},
          {'type':'tool_use','part':{'tool':'bash','state':{'status':'completed','input':{'command':'python tools/contrib/check.py'},'output':'diffs= 1 offsets=0x48'}}},
          {'type':'text','part':{'text':'sk-or-secretshouldnotshowhere'}}]
        log.write_text(''.join(json.dumps(x)+'\n' for x in lines))
        with sqlite3.connect(self.work/'spore.sqlite3') as db:
            db.execute('''INSERT INTO attempts(va,pass_number,status,effort,utc,log_path,run_id,worker_idx) VALUES('0x00000001',1,'FAIL','+2','now',?,'run1',0)''',(str(log),))
            db.execute("INSERT INTO attempts(va,pass_number,status,effort,utc,run_id,worker_idx) VALUES('0x00000001',2,'MATCH','','now','run1',0)")
            db.execute("INSERT INTO attempts(va,pass_number,status,effort,utc,run_id,worker_idx) VALUES('0x00000002',1,'FAIL','+4','now','run1',1)")
        d=snapshot(self.repo)
        self.assertEqual(d['counters']['matches'],1)
        self.assertEqual(d['counters']['fails'],1)
        self.assertEqual(d['counters']['pending'],1)
        self.assertEqual(d['effort']['+4'],1)
        self.assertEqual(d['effort']['+2'],0)
        self.assertAlmostEqual(d['costs']['MATCH']['cost'],0.005)
        self.assertEqual(d['costs']['MATCH']['covered'],1)
        self.assertEqual(d['costs']['MATCH']['attempts'],2)
        self.assertEqual(functions(self.repo,'MATCH')['total'],1)
        self.assertEqual(functions(self.repo,'PENDING')['total'],1)
        function=function_detail(self.repo,'0x00000001')
        self.assertEqual(len(function['history']),2)
        self.assertEqual(function['diff_status'],'MATCH: zero remaining byte differences')
        self.assertIn('sk-or-',log.read_text())
        live=stream(self.repo,log)
        self.assertNotIn('sk-or-secretshouldnotshowhere',str(live))
        self.assertIn('[REDACTED]',str(live))
        self.assertNotIn('python tools/contrib/check.py',str(live))
        self.assertAlmostEqual(log_cost(self.repo,str(log))['cost'],0.005)
        with self.assertRaises(ValueError):function_detail(self.repo,'../../passwd')

    def test_controls_never_spawn_without_enable_or_invalid_module(self):
        control=Controller(self.repo,enabled=False)
        with self.assertRaises(PermissionError):control.start({})
        with self.assertRaises(PermissionError):control.stop()
        control=Controller(self.repo,enabled=True)
        self.assertIn('SPGonzago',modules(self.repo))
        data={'module':'not-a-real-module','workers':3,'max_inflight':2,'passes':3,'minutes':5,'budget':20}
        with self.assertRaises(ValueError):control.start(data)
        data['module']='SPGonzago'
        data['workers']=200
        with self.assertRaises(ValueError):control.start(data)
        data['workers']=3
        with patch('spore_dashboard_control.subprocess.run') as run,patch('spore_dashboard_control.subprocess.Popen') as popen:
            run.return_value.returncode=0
            popen.return_value.pid=12345
            with patch.object(control,'_confirm_idle'):
                result=control.start(data)
            self.assertEqual(result['controller_pid'],12345)
            self.assertTrue(run.called)
            self.assertTrue(popen.called)
            argv=popen.call_args.args[0]
            self.assertIn('--module',argv)
            self.assertIn('SPGonzago',argv)
            self.assertIn('--max-inflight',argv)

if __name__=='__main__':unittest.main()
