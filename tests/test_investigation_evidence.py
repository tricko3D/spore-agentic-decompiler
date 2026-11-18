"""Read-only/no-model regression coverage for durable Spore investigation evidence.

Run: python3 -m unittest discover -s tests -p 'test_investigation_evidence.py' -v
"""
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'engine/spore_provider_fix/work/scratch/spore_investigation.py'
SPEC = importlib.util.spec_from_file_location('spore_investigation_under_test', SCRIPT)
investigation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(investigation)


class InvestigationEvidenceTests(unittest.TestCase):
    VA = '0x00e61d90'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'match/slices/s00e61d90/s00e61d90.cpp'
        self.source.parent.mkdir(parents=True)
        self.initial = '// @ 0x00e61d90\nint function() { return 1; }\n'
        self.source.write_text(self.initial)
        image = self.root / 'work/SporeApp.analysis.bin'
        image.parent.mkdir(parents=True)
        image.write_bytes(b'fake-image')
        disasm = self.root / 'tools/matching/disasm.py'
        disasm.parent.mkdir(parents=True)
        disasm.write_text('print("fake")\n')

    def tearDown(self):
        self.tmp.cleanup()

    def fake_disasm(self, command, **kwargs):
        self.assertEqual(command[-2:], [self.VA, '144'])
        return types.SimpleNamespace(
            returncode=0, stdout='  00e61d90  55  push ebp\n', stderr='')

    def test_baseline_is_immutable_while_latest_tracks_current_source(self):
        with mock.patch.object(investigation.subprocess, 'run',
                               side_effect=self.fake_disasm):
            folder = investigation.prepare(self.root, self.VA, 144, self.source)
            self.source.write_text(self.initial.replace('return 1', 'return 2'))
            investigation.prepare(self.root, self.VA, 144, self.source)
        self.assertEqual((folder / 'initial.cpp').read_text(), self.initial)
        self.assertIn('return 2', (folder / 'latest.cpp').read_text())
        self.assertIn('+int function() { return 2; }',
                      (folder / 'source-diff.patch').read_text())
        self.assertIn('push ebp', (folder / 'retail-assembly.txt').read_text())
        self.assertIn('keep.md', (folder / 'README.md').read_text())

    def test_missing_image_does_not_stop_investigation(self):
        (self.root / 'work/SporeApp.analysis.bin').unlink()
        folder = investigation.prepare(self.root, self.VA, 144, self.source)
        self.assertIn('unavailable', (folder / 'retail-assembly.txt').read_text())
        self.assertTrue((folder / 'initial.cpp').is_file())

    def test_checker_evidence_records_actual_execution_not_source_inspection(self):
        with mock.patch.object(investigation.subprocess, 'run',
                               side_effect=self.fake_disasm):
            folder = investigation.prepare(self.root, self.VA, 144, self.source)
        seen = set()
        inspect = {'type':'tool_use',
                   'part': {'tool':'shell','state': {'status':'completed',
                     'input':{'command':'sed -n 1,50p tools/contrib/check.py'},
                     'output':'checker source'}}}
        run = {'type':'tool_use',
               'part': {'tool':'shell','state': {'status':'completed',
                 'input': {'command': 'cd /tmp; .venv/bin/python tools/contrib/check.py '
                                        '0x00e61d90 --flags "/O2 /MD"'},
                 'output': 'NO MATCH size=138/144 diffs=6 offsets=0x2,0x3'}}}
        self.assertFalse(investigation.record_checker_event(self.root, self.VA, inspect, seen))
        self.assertTrue(investigation.record_checker_event(self.root, self.VA, run, seen))
        self.assertFalse(investigation.record_checker_event(self.root, self.VA, run, seen))
        text = (folder / 'experiments.md').read_text()
        self.assertEqual(text.count('NO MATCH'), 1)
        self.assertIn('diffs=6', text)
        self.assertNotIn('checker source', text)
        snapshots = list((folder / 'candidates').glob('*.cpp'))
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].read_text(), self.initial)
        self.source.write_text(self.initial.replace('return 1', 'return 2'))
        changed = {'type': 'tool_use', 'part': {'state': {'status': 'completed',
            'input': {'command': '.venv/bin/python tools/contrib/check.py 0x00e61d90'},
            'output': 'NO MATCH size=138/144 diffs=3'}}}
        self.assertTrue(investigation.record_checker_event(self.root, self.VA, changed, seen))
        self.assertEqual(len(list((folder / 'candidates').glob('*.cpp'))), 2)
        patch = list((folder / 'candidate-diffs').glob('*.patch'))
        self.assertEqual(len(patch), 1)
        self.assertIn('+int function() { return 2; }', patch[0].read_text())

    def test_provider_retry_same_pass_can_resume_new_pass_cannot(self):
        self.assertIsNone(investigation.session_for_pass(
            self.root, self.VA, 4, 'ses_old'))
        investigation.remember_session(self.root, self.VA, 4, 'ses_new')
        self.assertEqual(investigation.session_for_pass(
            self.root, self.VA, 4, 'ses_new'), 'ses_new')
        self.assertIsNone(investigation.session_for_pass(
            self.root, self.VA, 5, 'ses_new'))
        self.assertIsNone(investigation.session_for_pass(
            self.root, self.VA, 4, 'ses_other'))

    def test_pass_result_preserves_evidence_and_working_source(self):
        with mock.patch.object(investigation.subprocess, 'run',
                               side_effect=self.fake_disasm):
            folder = investigation.prepare(self.root, self.VA, 144, self.source)
        investigation.final_result(self.root, self.VA, 4, 'FAIL',
                                   'NO MATCH size 138/144')
        self.assertIn('Pass 4 independently checked: FAIL',
                      (folder / 'experiments.md').read_text())
        self.assertEqual(self.source.read_text(), self.initial)

    def test_does_not_write_original_cpp_back_into_working_file(self):
        with mock.patch.object(investigation.subprocess, 'run',
                               side_effect=self.fake_disasm):
            folder = investigation.prepare(self.root, self.VA, 144, self.source)
        (folder / 'initial.cpp').write_text('Different archived contents')
        self.source.write_text(self.initial.replace('return 1', 'return 123'))
        investigation.refresh(self.root, self.VA)
        self.assertIn('return 123', self.source.read_text())

    def test_runner_wires_evidence_and_compaction_without_a_step_cutoff(self):
        text = (ROOT / 'engine/spore_provider_fix/work/scratch/spore_first_pass_sqlite.py').read_text()
        self.assertIn('investigation.prepare(ROOT, va', text)
        self.assertIn('investigation.session_for_pass(', text)
        self.assertIn('investigation.record_checker_event(', text)
        self.assertIn("'compaction': {'auto': True", text)
        self.assertNotIn('MAX_INFERENCE_STEPS', text)


if __name__ == '__main__':
    unittest.main()
