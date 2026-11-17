"""Offline end-to-end setup checks, no OpenRouter requests or compiler work."""
import os
import json
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

CLI = Path(__file__).resolve().parents[1] / 'spore_agentic.py'


class StandaloneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'upstream'
        contrib = self.repo / 'tools/contrib'
        contrib.mkdir(parents=True)
        (contrib / 'task.py').write_text('''
def fingerprints(): return {0x12345678: {"size": 18}, 0x12345679: {"size": 32}}
def blocked(): return set()
def library_functions(): return set()
def marked(): return set()
def new_source_path(va): return "src/game/SPGonzago/test.cpp"
''')
        (contrib / 'check.py').write_text('''import os,sys
ROOT='unused'
def main(): return 0
if __name__ == "__main__":
    sys.exit(main())
''')
        modmap = self.repo / 'work/migrate/modmap.json'
        modmap.parent.mkdir(parents=True)
        modmap.write_text('{"functions": {"12345678": {"module":"SPGonzago","confidence":0.9}, "12345679": {"module":"Unrelated","confidence":1.0}}}')
        bindir = self.root / 'bin'
        bindir.mkdir()
        opencode = bindir / 'opencode'
        opencode.write_text('#!/bin/sh\nexit 0\n')
        opencode.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update({
            'XDG_CONFIG_HOME': str(self.root/'config'),
            'OPENROUTER_API_KEY': 'sk-or-test-not-a-real-key',
            'PATH': str(bindir)+os.pathsep+self.env['PATH'],
        })

    def cli(self, *args):
        return subprocess.run([sys.executable, str(CLI), *args], cwd=self.root,
                              env=self.env, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)

    def test_setup_preserves_state_and_checker(self):
        a = self.cli('setup', '--repo', str(self.repo))
        self.assertEqual(a.returncode, 0, a.stdout)
        db = self.repo/'work/scratch/codex-pass1/spore.sqlite3'
        self.assertTrue(db.exists())
        with sqlite3.connect(db) as conn:
            self.assertEqual(conn.execute('select count(*) from queue').fetchone()[0], 1)
            conn.execute("INSERT INTO attempts(va,pass_number,status,utc) VALUES('0x12345678',1,'FAIL','now')")
            conn.commit()
        b = self.cli('setup', '--repo', str(self.repo))
        self.assertEqual(b.returncode, 0, b.stdout)
        with sqlite3.connect(db) as conn:
            self.assertEqual(conn.execute('select count(*) from attempts').fetchone()[0], 1)
        checker = (self.repo/'tools/contrib/check.py').read_text()
        self.assertEqual(checker.count('SPGONZAGO_SQLITE_CHECK_LOCK_V1'), 1)
        self.assertEqual(self.cli('plan').returncode, 0)
        self.assertEqual(self.cli('recover').returncode, 0)
        self.assertEqual(self.cli('doctor').returncode, 0)
        dest = self.root/'diagnostics.zip'
        export = self.cli('diagnostics', '--output', str(dest))
        self.assertEqual(export.returncode, 0, export.stdout)
        self.assertTrue(dest.is_file())
        from zipfile import ZipFile
        with ZipFile(dest) as z:
            self.assertIn('state/spore.sqlite3', z.namelist())
            report = json.loads(z.read('state/provider-evidence.json'))
            self.assertEqual(report['counts']['proven_provider_only'], 0)
            self.assertEqual(report['counts']['unverified'], 1)
            self.assertNotIn('openrouter.key', ' '.join(z.namelist()))


    def test_diagnostics_exports_provider_evidence_without_raw_logs(self):
        a = self.cli('setup', '--repo', str(self.repo))
        self.assertEqual(a.returncode, 0, a.stdout)
        from zipfile import ZipFile
        logs = self.repo/'work/scratch/codex-pass1/logs'
        logs.mkdir(parents=True)
        log = logs/'12345678.deepseek-openrouter.jsonl'
        now = datetime.now(timezone.utc)
        t = int(now.timestamp()*1000)
        log.write_text('\\n'.join(json.dumps(event) for event in (
            {'type': 'step_start', 'timestamp': t},
            {'type': 'retry', 'timestamp': t + 1000, 'error': {
                'type': 'provider.rate-limit', 'status': 200,
                'response': {'body': json.dumps({'error': {
                    'code': 429, 'metadata': {
                        'error_type': 'rate_limit_exceeded',
                        'provider_code': 'ttft_deadline_guard'}}})}}},
        ))+'\\n')
        db = self.repo/'work/scratch/codex-pass1/spore.sqlite3'
        with sqlite3.connect(db) as conn:
            conn.execute('''INSERT INTO attempts(va,pass_number,status,utc,log_path)
                         VALUES(?,?,?,?,?)''',
                         ('0x12345678', 1, 'DEFER', now.isoformat(), str(log)))
            conn.commit()
        dest = self.root/'provider-diagnostics.zip'
        exported = self.cli('diagnostics', '--output', str(dest))
        self.assertEqual(exported.returncode, 0, exported.stdout)
        with ZipFile(dest) as z:
            self.assertNotIn('checkout/work/scratch/codex-pass1/logs/12345678.deepseek-openrouter.jsonl', z.namelist())
            result = json.loads(z.read('state/provider-evidence.json'))
            self.assertEqual(result['counts']['proven_provider_only'], 1)
            self.assertEqual(result['counts']['unverified'], 0)
            self.assertEqual(result['attempts'][0]['classification'], 'PROVEN_PROVIDER_ONLY')


    def test_rejects_partial_queue(self):
        work = self.repo/'work/scratch/codex-pass1'
        work.mkdir(parents=True)
        (work/'queue.csv').write_text('va,size,suggested_file\n')
        a = self.cli('setup', '--repo', str(self.repo))
        self.assertNotEqual(a.returncode, 0)
        self.assertIn('Only one of queue.csv/results.csv exists', a.stdout)


if __name__ == '__main__':
    unittest.main()
