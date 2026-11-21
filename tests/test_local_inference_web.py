"""Live per-Agent local decoding rates from llama.cpp, no separate slot grid."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import spore_dashboard_data as data


class Reply:
    def __init__(self,rows):
        self.payload=json.dumps(rows).encode()
    def __enter__(self):
        return self
    def __exit__(self,*args):
        return False
    def read(self):
        return self.payload


class LiveAgentThroughputTests(unittest.TestCase):
    def setUp(self):
        data._LOCAL_SLOT_SAMPLES.clear()
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo=Path(self.temp.name)
        self.rid='test123'
        folder=self.repo/'work/scratch/codex-pass1/parallel'/self.rid
        folder.mkdir(parents=True)
        (folder/'run.json').write_text('{"backend":"local"}')
        self.workers=[
            {'worker':1,'alive':True,'current':'0x00c7a7d0','phase':'PROBING','tps':None},
            {'worker':2,'alive':True,'current':'0x00d47770','phase':'PROBING','tps':None}]
    def poll(self,rows,second):
        with mock.patch.object(data,'urlopen',return_value=Reply(rows)),\
             mock.patch.object(data.time,'monotonic',return_value=second):
            data.live_agent_throughput(self.repo,self.rid,self.workers)

    @staticmethod
    def slot(sid,task,count,target,as_list=True):
        prompt='HIGH-EFFORT LOCAL BYTE-EXACT DECOMPILATION, TARGET '+target+'.'
        nxt=[{'n_decoded':count}] if as_list else {'n_decoded':count}
        return {'id':sid,'id_task':task,'is_processing':True,
                'prompt':prompt,'next_token':nxt}

    def test_live_slot_array_counts_are_attributed_by_function(self):
        self.poll([self.slot(12,91,0,'0x00d47770'),
                   self.slot(3,87,0,'0x00c7a7d0')],100.)
        self.assertEqual(self.workers[0]['phase'],'PREFILL')
        self.assertEqual(self.workers[1]['tps'],0)
        self.poll([self.slot(12,91,42,'0x00d47770'),
                   self.slot(3,87,60,'0x00c7a7d0')],103.)
        self.assertEqual([w['tps'] for w in self.workers],[20.,14.])
        self.assertEqual(self.workers[0]['phase'],'GENERATING')

    def test_dict_shape_and_task_reset(self):
        self.poll([self.slot(0,11,0,'0x00c7a7d0',False)],100.)
        self.poll([self.slot(0,11,30,'0x00c7a7d0',False)],103.)
        self.assertEqual(self.workers[0]['tps'],10.)
        self.workers[0]['tps']=None
        self.poll([self.slot(0,12,3,'0x00c7a7d0',False)],106.)
        self.assertIsNone(self.workers[0]['tps'])

    def test_prompt_identity_prevents_wrong_worker_speed(self):
        self.poll([self.slot(0,1,50,'0x00001111')],100.)
        self.assertIsNone(self.workers[0]['tps'])
        self.assertIsNone(self.workers[1]['tps'])
        self.assertIsNone(data._slot_target('0x00c7a7d0 and 0x00d47770',
                          {'0x00c7a7d0':1,'0x00d47770':2}))

    def test_previous_reading_is_cleared_when_slot_idle(self):
        self.poll([self.slot(0,1,0,'0x00c7a7d0')],100.)
        self.poll([{'id':0,'is_processing':False}],103.)
        self.assertEqual(data._LOCAL_SLOT_SAMPLES,{})

    def test_nonlocal_backend_does_not_poll(self):
        folder=self.repo/'work/scratch/codex-pass1/parallel'/self.rid
        (folder/'run.json').write_text('{"backend":"openrouter"}')
        with mock.patch.object(data,'urlopen',side_effect=AssertionError('unexpected')):
            data.live_agent_throughput(self.repo,self.rid,self.workers)

    def test_no_separate_model_slot_grid_or_route(self):
        source=(ROOT/'dashboard_web/src/views/Operations.tsx').read_text()
        self.assertIn('d?.workers?.map(w=>',source)
        self.assertIn('w.tps',source)
        self.assertNotIn('inference.slots.map',source)
        self.assertNotIn('Local inference slots',source)
        self.assertNotIn('/api/local-inference',(ROOT/'dashboard/server.py').read_text())

if __name__=='__main__':
    unittest.main()
