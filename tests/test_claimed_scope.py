"""No-inference tests for SAD's GitHub-claimant scope.

Run with: python3 -m unittest discover -s tests -p 'test_claimed_scope.py' -v
No network, model API, or real Spore checkout is required.
"""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
FILE = ROOT / 'spore_dashboard_control.py'
spec = importlib.util.spec_from_file_location('test_sad_dashboard_control', FILE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

CLAIMS = """# Functions in open pull requests (claims). Generated 2026-11-18 09:57 UTC from 3 open pull requests.
# address    pr     author                 last update
0x01213b70  #446   tricko3D               2026-11-18
0x01213be0  #446   tricko3D               2026-11-18
0x01213b70  #449   tricko3D               2026-11-18
0x00e61d90  #441   supercid87             2026-11-18
0x005c9070  #443   davurphy               2026-11-18
"""


class ClaimsScopeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        self.controller = module.Controller(self.repo, enabled=True)
        self.old_env = mock.patch.dict(module.os.environ,
                                       {'SPORE_RETRY_ALLOWLIST': ''})
        self.old_env.start()
        self.requests = []
        def get_url(request, timeout=0):
            self.requests.append(request.full_url)
            return io.BytesIO(CLAIMS.encode('utf-8'))
        self.urlopen = mock.patch.object(module.urllib.request, 'urlopen',
                                         side_effect=get_url)
        self.urlopen.start()

    def tearDown(self):
        self.urlopen.stop()
        self.old_env.stop()
        self.temporary.cleanup()

    def test_claimants_come_from_exact_upstream_registry(self):
        users = self.controller.claimants()
        self.assertEqual([(u['name'], u['count']) for u in users['users']],
                         [('tricko3D', 2), ('davurphy', 1), ('supercid87', 1)])
        self.assertEqual(self.requests, [module.CLAIMS_URL])
        self.assertIn('NoahBPeterson/spore-decomp/claims/claims.txt',
                      users['source'])

    def test_repeated_claims_deduplicate_addresses(self):
        snapshot = self.controller._claim_scope('tricko3D')
        path = Path(snapshot['path'])
        self.addCleanup(path.unlink, missing_ok=True)
        self.assertEqual(snapshot['count'], 2)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')),
                         ['0x01213b70', '0x01213be0'])
        self.assertNotEqual(path.name, 'retry-all.json')

    def test_user_lookup_is_case_insensitive(self):
        snapshot = self.controller._claim_scope('TRICKO3D')
        self.addCleanup(Path(snapshot['path']).unlink, missing_ok=True)
        self.assertEqual(snapshot['claim_user'], 'tricko3D')

    def test_missing_user_blocks_paid_scope(self):
        with self.assertRaisesRegex(ValueError, 'Select a GitHub claimant'):
            self.controller._claim_scope(None)
        with self.assertRaisesRegex(ValueError, 'no current open claims'):
            self.controller._claim_scope('unclaimed-user')

    def test_upstream_failure_does_not_fall_back_to_all_modules(self):
        self.controller.claim_cache = None
        with mock.patch.object(module.urllib.request, 'urlopen',
                               side_effect=OSError('offline')):
            with self.assertRaisesRegex(ValueError, 'Cannot refresh upstream claims'):
                self.controller._claim_scope('tricko3D', force=True)

    def test_unexpected_registry_format_fails_closed(self):
        with mock.patch.object(module.urllib.request, 'urlopen',
                               return_value=io.BytesIO(b'garbled content')):
            with self.assertRaisesRegex(ValueError, 'unexpected format'):
                self.controller.claimants()

    def test_preview_uses_only_claimed_addresses_then_removes_temp_file(self):
        captured = {}
        def free_plan(cmd, **kwargs):
            self.assertIn('--all-modules', cmd)
            self.assertNotIn('--module', cmd)
            path = Path(kwargs['env']['SPORE_RETRY_ALLOWLIST'])
            self.assertTrue(path.exists())
            captured['path'] = path
            captured['addresses'] = json.loads(path.read_text())
            return types.SimpleNamespace(
                returncode=0,
                stdout='All confidently classified runnable functions '
                       '(passes are additional per run): 1\n'
                       'SPORE_ALLOWLIST_BREAKDOWN=' +
                       json.dumps({'listed': 2, 'eligible': 1, 'excluded': 1,
                                   'reasons': {'already_manifested': 1}}) + '\n',
                stderr='')

        # The real config loader might read local settings, which this test
        # should not require.
        with mock.patch('spore_agentic.config',
                        return_value={'min_confidence': 0.70}):
            with mock.patch.object(module.subprocess, 'run', side_effect=free_plan):
                preview = self.controller.preview('__claimed__', 4, 'tricko3D')
        self.assertEqual(captured['addresses'], ['0x01213b70', '0x01213be0'])
        self.assertFalse(captured['path'].exists())
        self.assertEqual(preview['eligible'], 1)
        self.assertEqual(preview['claim_user'], 'tricko3D')
        self.assertEqual(preview['claim_count'], 2)

    def test_launch_passes_snapshot_to_runner_not_original_allowlist(self):
        existing = self.repo / 'work/scratch/codex-pass1/retry-all.json'
        existing.parent.mkdir(parents=True)
        existing.write_text('["0x00000010"]\n')
        controller = self.controller
        controller._confirm_idle = lambda: None
        controller._launch_environment = lambda: {}
        controller._provider_key_preflight = lambda env, budget: None
        snapshots = []
        def fake_run(cmd, **kwargs):
            self.assertIn('--all-modules', cmd)
            return types.SimpleNamespace(returncode=0, stdout='ok', stderr='')
        def fake_popen(cmd, **kwargs):
            self.assertIn('--all-modules', cmd)
            path = Path(kwargs['env']['SPORE_RETRY_ALLOWLIST'])
            self.assertTrue(path.exists())
            snapshots.extend(json.loads(path.read_text()))
            self.addCleanup(path.unlink, missing_ok=True)
            return types.SimpleNamespace(pid=123456)

        settings = {'module': '__claimed__', 'claim_user': 'supercid87',
                    'workers': 2, 'max_inflight': 2, 'passes': 2,
                    'minutes': 20, 'budget': 1.0}
        with mock.patch.object(module.subprocess, 'run', side_effect=fake_run):
            with mock.patch.object(module.subprocess, 'Popen',
                                   side_effect=fake_popen):
                result = controller.start(settings)
        self.assertTrue(result['started'])
        self.assertEqual(result['claim_user'], 'supercid87')
        self.assertEqual(snapshots, ['0x00e61d90'])
        self.assertEqual(json.loads(existing.read_text()), ['0x00000010'])
        self.assertEqual(controller.launch_settings['allowlist_count'], 1)

    def test_no_username_fails_before_any_worker_is_spawned(self):
        self.controller._confirm_idle = lambda: None
        data = {'module': '__claimed__', 'workers': 1, 'max_inflight': 1,
                'passes': 3, 'minutes': 20, 'budget': 1.0}
        with mock.patch.object(module.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(ValueError, 'Select a GitHub claimant'):
                self.controller.start(data)
            launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
