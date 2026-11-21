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
        self.folder=self.repo/'work/scratch/codex-pass1/parallel'/self.rid
        self.folder.mkdir(parents=True)
        (self.folder/'run.json').write_text('{"backend":"local"}')
        self.workers=[
            {'worker':1,'alive':True,'current':'0x00c7a7d0','phase':'WAITING','tps':None},
            {'worker':2,'alive':True,'current':'0x00d47770','phase':'WAITING','tps':None}]
    def poll(self,rows,second):
        with mock.patch.object(data,'urlopen',return_value=Reply(rows)),\
             mock.patch.object(data.time,'monotonic',return_value=second):
            return data.live_agent_throughput(self.repo,self.rid,self.workers)

    def slot(self,sid,task,count,worker_idx=None,as_list=True):
        nxt=[{'n_decoded':count}] if as_list else {'n_decoded':count}
        item={'id':sid,'id_task':task,'is_processing':True,'next_token':nxt}
        if worker_idx is not None:
            item['params']={'seed':data._local_slot_seed(self.rid,worker_idx)}
        return item

    def test_live_slots_without_prompt_are_attributed_to_workers_by_seed(self):
        self.poll([self.slot(12,91,0,1),self.slot(3,87,0,0)],100.)
        self.assertEqual([w['phase'] for w in self.workers],['PREFILL','PREFILL'])
        aggregate=self.poll([self.slot(12,91,42,1),
                             self.slot(3,87,60,0)],103.)
        self.assertEqual([w['tps'] for w in self.workers],[20.,14.])
        self.assertEqual([w['phase'] for w in self.workers],['GENERATING','GENERATING'])
        self.assertEqual(aggregate,34.)

    def test_slot_without_identity_contributes_to_aggregate_only(self):
        self.poll([self.slot(0,7,0,None)],100.)
        result=self.poll([self.slot(0,7,20,None)],102.)
        self.assertEqual(result,10.)
        self.assertTrue(all(w['tps'] is None for w in self.workers))
        self.assertTrue(all(w['phase']=='WAITING' for w in self.workers))

    def test_task_reset_does_not_invent_speed(self):
        self.poll([self.slot(0,11,0,0,False)],100.)
        self.poll([self.slot(0,11,30,0,False)],103.)
        self.assertEqual(self.workers[0]['tps'],10.)
        self.workers[0]['tps']=None
        self.assertIsNone(self.poll([self.slot(0,12,3,0,False)],106.))
        self.assertIsNone(self.workers[0]['tps'])

    def test_previous_reading_clears_on_idle(self):
        self.poll([self.slot(0,1,0,0)],100.)
        self.poll([{'id':0,'is_processing':False}],103.)
        self.assertEqual(data._LOCAL_SLOT_SAMPLES,{})

    def test_local_status_does_not_use_openrouter_verification_flag(self):
        (self.folder/'run.json').write_text(
            '{"backend":"local","processes":[{"worker":0,"pid":123}]}')
        info=data.worker_info(self.repo,self.rid,
                              {0:'0x00c7a7d0'},active_slots={'0x00c7a7d0':False})
        self.assertEqual(info[0]['phase'],'WAITING')
        self.assertNotEqual(info[0]['phase'],'PROBING')

    def test_nonlocal_backend_does_not_poll(self):
        (self.folder/'run.json').write_text('{"backend":"openrouter"}')
        with mock.patch.object(data,'urlopen',side_effect=AssertionError('unexpected')):
            self.assertIsNone(data.live_agent_throughput(self.repo,self.rid,self.workers))

    def test_seed_matches_model_router(self):
        sys.path.append(str(ROOT/'engine/spore_provider_fix/work/scratch'))
        import spore_model_router
        self.assertEqual(data._local_slot_seed(self.rid,17),
                         spore_model_router.local_slot_seed(self.rid,17))

    def test_no_separate_model_slot_grid_or_route(self):
        source=(ROOT/'dashboard_web/src/views/Operations.tsx').read_text()
        self.assertIn('d?.workers?.map(w=>',source)
        self.assertIn('w.tps',source)
        self.assertNotIn('inference.slots.map',source)
        self.assertNotIn('Local inference slots',source)
        self.assertNotIn('/api/local-inference',(ROOT/'dashboard/server.py').read_text())


if __name__=='__main__':
    unittest.main()
