"""Independent checker metrics must survive long model notes in the SQLite runner."""
from pathlib import Path
import importlib.util
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
ENGINE=ROOT/'engine/spore_provider_fix/work/scratch'
sys.path.insert(0,str(ENGINE))
spec=importlib.util.spec_from_file_location('spore_checker_runner_for_test',ENGINE/'spore_first_pass_sqlite.py')
runner=importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
from spore_dashboard_data import byte_agreement


class CheckerEvidenceTests(unittest.TestCase):
    def test_metrics_and_note_preservation(self):
        output=['NO MATCH 0x00ab0100 ?long@@Z [/O2]', 'compiled candidate',
                'size=59/59 diffs= 1 offsets=0x48']
        metrics=runner.checker_metric_summary(output)
        self.assertEqual(metrics,'size=59/59 diffs=1')
        note='Pass 2: '+('model technical reason '*120)+' | checker: '+metrics+' | NO MATCH 0x00ab0100'
        preserved=runner.compact_attempt_note(note)
        self.assertLessEqual(len(preserved),1200)
        self.assertIn('checker: size=59/59 diffs=1',preserved)
        self.assertEqual(byte_agreement('FAIL',59,preserved)['matched'],58)

    def test_no_fake_agreement(self):
        self.assertEqual(runner.checker_metric_summary(['NO MATCH without metrics']),'')
        self.assertEqual(runner.compact_attempt_note('a'*2000), 'a'*1200)
        mismatch=runner.checker_metric_summary(['size=58/59','diffs=1'])
        self.assertEqual(mismatch,'size=58/59 diffs=1')
        result=byte_agreement('FAIL',59,'Pass 1: mismatch | checker: '+mismatch)
        self.assertIsNone(result['matched'])
        self.assertEqual(result['kind'],'size_mismatch')

if __name__=='__main__':
    unittest.main()
