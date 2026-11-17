"""Live JSONL watchdog tests; no paid inference or network."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ENGINE=Path(__file__).resolve().parents[1] / 'engine/spore_provider_fix/work/scratch'
sys.path.insert(0,str(ENGINE))
import spore_progress_watch as watch


class ProgressWatchTests(unittest.TestCase):
    def spawn(self, log, lines, pause=3):
        code="""import json,sys,time
with open(sys.argv[1],'ab',buffering=0) as out:
 for item in json.loads(sys.argv[2]):
  out.write((json.dumps(item)+'\\n').encode())
time.sleep(float(sys.argv[3]))
"""
        return subprocess.Popen([sys.executable,'-c',code,str(log),json.dumps(lines),str(pause)],
                                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                start_new_session=True)

    def test_verified_output_token_raises_once(self):
        with tempfile.TemporaryDirectory() as t:
            log=Path(t)/'agent.jsonl'
            lines=[{'type':'step_start'},{'type':'step_finish','part':{'tokens':{'output':61}}},
                   {'type':'step_finish','part':{'tokens':{'output':20}}}]
            proc=self.spawn(log,lines,pause=0.1)
            calls=[]
            try:
                result,code=watch.watch(proc,log,1,on_output=lambda:calls.append(True),poll_seconds=0.02)
                self.assertEqual(result,'complete')
                self.assertEqual(code,0)
                self.assertEqual(len(calls),1)
            finally:
                watch.stop_process_group(proc)

    def test_retry_without_tokens_does_not_keep_stalled_agent_alive(self):
        with tempfile.TemporaryDirectory() as t:
            log=Path(t)/'agent.jsonl'
            proc=self.spawn(log,[{'type':'retry','error':{'response':{'body':'{}'}}}],pause=10)
            try:
                result,_=watch.watch(proc,log,0.15,on_output=lambda:self.fail('no tokens'),poll_seconds=0.02)
                self.assertEqual(result,'idle')
                self.assertIsNotNone(proc.poll())
            finally:
                watch.stop_process_group(proc)

    def test_confirmed_429_aborts_only_failing_inference(self):
        with tempfile.TemporaryDirectory() as t:
            log=Path(t)/'agent.jsonl'
            lines=[{'type':'retry','error':{'response':{'body':json.dumps({'error':{'code':429}})}}}]
            proc=self.spawn(log,lines,pause=10)
            try:
                result,_=watch.watch(proc,log,1,poll_seconds=0.02)
                self.assertEqual(result,'provider_429')
                self.assertIsNotNone(proc.poll())
            finally:
                watch.stop_process_group(proc)


if __name__=='__main__':unittest.main()
