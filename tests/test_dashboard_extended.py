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

    def test_worker_details_byte_agreement_and_reasons(self):
        from spore_dashboard_data import byte_agreement
        note='Pass 1: ABI mismatch | checker: NO MATCH size=59/59 diffs=1 offsets=0x48'
        self.assertEqual(byte_agreement('FAIL',59,note)['matched'],58)
        mismatch=byte_agreement('FAIL',59,'Pass 1: size mismatch | checker: NO MATCH size=58/59 diffs=1 offsets=0x48')
        self.assertEqual(mismatch['kind'],'size_mismatch')
        self.assertIsNone(mismatch['matched'])
        self.assertIsNone(byte_agreement('FAIL',59,'Pass 1: guessed one diff'))
        # The runner stores timeout evidence without the " | checker:" delimiter.
        timeout='Pass 2: time limit (5 min); size=59/59 diffs=1 | NO MATCH'
        deferred=byte_agreement('DEFER',59,timeout)
        self.assertEqual(deferred['matched'],58)
        self.assertEqual(deferred['kind'],'independent_checker')
        self.assertIsNone(byte_agreement('DEFER',59,'Pass 2: time limit (5 min); No candidate verified'))
        self.assertIsNone(byte_agreement('FAIL',59,'Pass 2: model claims size=59/59 diffs=1'))
        idle=agent_detail(self.repo,1)
        self.assertIsNone(idle['va'])
        self.assertIsNone(idle['bytes'])
        self.assertEqual(idle['events'],[])
        with sqlite3.connect(self.work/'spore.sqlite3') as db:
            db.execute("INSERT INTO attempts(va,pass_number,status,effort,note,utc,run_id,worker_idx) VALUES('0x00000002',1,'FAIL','+2',?,'2026-11-17T21:00:00','run1',1)",
                       ('Pass 1: EBP parent-frame ABI and EH handler differ | checker: NO MATCH size=31/31 diffs=1 offsets=0x12',))
            db.execute("INSERT INTO attempts(va,pass_number,status,effort,note,utc,run_id,worker_idx) VALUES('0x00000001',1,'MATCH','',?,'2026-11-17T21:00:00','run1',0)",
                       ('Pass 1: independently byte-exact',))
        failure=functions(self.repo,'FAIL')['items'][0]
        self.assertEqual(failure['agreement']['matched'],30)
        self.assertIn('EBP parent-frame ABI',failure['reason'])
        detail=function_detail(self.repo,'0x00000002')
        self.assertEqual(detail['agreement']['total'],31)
        self.assertIn('EH handler differ',detail['reason'])
        self.assertEqual(functions(self.repo,'MATCH')['items'][0]['agreement']['matched'],20)
        agent=agent_detail(self.repo,2)
        self.assertEqual(agent['va'],'0x00000002')
        self.assertEqual(agent['bytes'],31)
        data=snapshot(self.repo)
        self.assertEqual(data['bytes']['queue'],68)
        self.assertEqual(data['bytes']['MATCH'],20)
        self.assertEqual(data['bytes']['FAIL'],31)
        self.assertEqual(data['bytes']['PENDING'],17)
        self.assertEqual(data['effort_bytes']['+2'],31)
        self.assertEqual(data['run']['bytes_target'],68)
        self.assertEqual(data['run']['bytes_completed'],20)
        html=(ROOT/'spore_dashboard_ui.html').read_text()
        self.assertIn("w.alive?'LIVE':'OFFLINE'",html)
        self.assertIn('Latest recorded reason',html)

    def test_historical_tool_checker_fallback_and_size_mismatch(self):
        logs=self.work/'logs'
        logs.mkdir()
        path=logs/'00000002.deepseek-openrouter.jsonl'
        def checker(output):
            return json.dumps({'type':'tool_use','part':{'tool':'bash','state':{
                'status':'completed','input':{'command':'python tools/contrib/check.py 0x00000002'},
                'output':output}}})+'\n'
        path.write_text(checker('NO MATCH 0x00000002\nsize=31/31 diffs= 1 offsets=0x10'))
        with sqlite3.connect(self.work/'spore.sqlite3') as db:
            db.execute("INSERT INTO attempts(va,pass_number,status,effort,note,utc,log_path,run_id,worker_idx) VALUES('0x00000002',1,'FAIL','+4',?,'now',?,'run1',1)",
                       ('Pass 1: parent-frame ABI | checker: NO MATCH 0x00000002',str(path)))
        row=functions(self.repo,'FAIL')['items'][0]
        self.assertEqual(row['agreement']['kind'],'logged_checker')
        self.assertEqual(row['agreement']['matched'],30)
        self.assertIn('parent-frame ABI',row['reason'])
        self.assertEqual(function_detail(self.repo,'0x00000002')['agreement']['matched'],30)
        path.write_text(checker('NO MATCH 0x00000002\nsize=30/31 diffs=1'))
        row=functions(self.repo,'FAIL')['items'][0]
        self.assertIsNone(row['agreement']['matched'])
        self.assertEqual(row['agreement']['compiled_size'],30)

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

    def test_dashboard_uses_rotated_saved_key_instead_of_stale_environment(self):
        import hashlib
        with sqlite3.connect(self.work/'spore.sqlite3') as db:
            fingerprint=hashlib.sha256(b'sk-or-new').hexdigest()[:20]
            db.execute("INSERT OR REPLACE INTO budget(singleton,key_fingerprint,baseline_usage,last_usage,updated_utc) VALUES(1,?,?,?,?)",
                       (fingerprint,0,0,'now'))
        saved_key=self.repo/'openrouter.key'
        saved_key.write_text('sk-or-new\n')
        control=Controller(self.repo,enabled=True)
        with patch('spore_agentic.KEY_FILE',saved_key),patch.dict(os.environ,{'OPENROUTER_API_KEY':'sk-or-old'}):
            env=control._launch_environment()
            self.assertEqual(env['OPENROUTER_API_KEY'],'sk-or-new')
        saved_key.unlink()
        with patch('spore_agentic.KEY_FILE',saved_key),patch.dict(os.environ,{'OPENROUTER_API_KEY':'sk-or-old'}):
            with self.assertRaisesRegex(ValueError,'outdated OPENROUTER_API_KEY'):
                control._launch_environment()
        with patch.dict(os.environ,{'OPENROUTER_API_KEY':'sk-or-new'}):
            self.assertEqual(control._launch_environment()['OPENROUTER_API_KEY'],'sk-or-new')


    def test_snapshot_reports_actual_verified_slots(self):
        folder=self.work/'parallel/run1'
        folder.mkdir(parents=True,exist_ok=True)
        (folder/'run.json').write_text(json.dumps({'processes':[{'worker':0,'pid':999999}]}))
        with sqlite3.connect(self.work/'spore.sqlite3') as db:
            db.execute("INSERT INTO inference_slots(id,run_id,va,created_utc,verified) VALUES('test-slot','run1','0x00000001','now',1)")
            db.execute("INSERT INTO claims(va,run_id,worker_idx,pid,started_utc) VALUES('0x00000001','run1',0,999999,'now')")
        result=snapshot(self.repo)
        self.assertEqual(result['workers'][0]['phase'],'VERIFIED')


if __name__=='__main__':unittest.main()
