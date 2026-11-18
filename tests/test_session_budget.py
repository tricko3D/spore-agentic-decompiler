"""Regression tests for per-run spending caps, independent of OpenRouter.

Run with: python3 -m unittest discover -s tests -p 'test_session_budget.py' -v
These tests use a temporary SQLite database and never make paid API calls.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import tempfile
import unittest


ENGINE = (Path(__file__).resolve().parents[1] /
          'engine/spore_provider_fix/work/scratch/spore_sqlite_store.py')


class SessionBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        spec = importlib.util.spec_from_file_location('isolated_spore_store', ENGINE)
        self.store = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.store)
        self.store.WORK = Path(self.temp.name)
        self.store.DB = self.store.WORK / 'spore.sqlite3'
        self.store.init_schema()

    def tearDown(self):
        self.temp.cleanup()

    def test_existing_project_spend_does_not_consume_new_run(self):
        with self.store.connect() as db:
            db.execute('INSERT INTO budget VALUES(1,?,?,?,?)',
                       ('key', 0.0, 13.0493, self.store.utc()))
        allowed, spent, reserved = self.store.update_budget(
            'key', 13.0493, 5.0, 10.0, run_id='run-A')
        self.assertTrue(allowed)
        self.assertEqual(spent, 0.0)
        self.assertEqual(reserved, 0.0)
        with self.store.connect() as db:
            project = db.execute('SELECT * FROM budget WHERE singleton=1').fetchone()
            self.assertAlmostEqual(project['last_usage']-project['baseline_usage'], 13.0493)

    def test_second_run_gets_new_baseline_without_erasing_first(self):
        self.store.update_budget('key', 13.0, 5.0, 10.0, run_id='run-A')
        self.store.update_budget('key', 13.7, 5.0, 10.0, run_id='run-A')
        a1 = self.store.update_budget('key', 13.7, 5.0, 10.0, run_id='run-A')
        b1 = self.store.update_budget('key', 13.7, 5.0, 10.0, run_id='run-B')
        self.assertAlmostEqual(a1[1], 0.7)
        self.assertEqual(b1[1], 0.0)
        self.assertAlmostEqual(
            self.store.update_budget('key', 13.7, 5.0, 10.0, run_id='run-A')[1],
            0.7)

    def test_worker_restart_does_not_reset_same_run_baseline(self):
        self.store.update_budget('key', 15.0, 5.0, 10.0, run_id='same-run')
        self.store.update_budget('key', 16.0, 5.0, 10.0, run_id='same-run')
        still = self.store.update_budget('key', 15.5, 5.0, 10.0, run_id='same-run')
        self.assertAlmostEqual(still[1], 1.0)

    def test_external_openrouter_limit_blocks_new_run(self):
        allowed, spent, _ = self.store.update_budget(
            'key', 13.0493, 5.0, 0.0726, run_id='run-new')
        self.assertEqual(spent, 0.0)
        self.assertFalse(allowed, 'Provider limit must still override new per-run cap')

    def test_reservations_do_not_exceed_session_limit(self):
        self.store.update_budget('key', 7.0, 0.25, 100.0, run_id='run-limit')
        first = self.store.reserve_budget('run-limit', '0x00100000', 0.1, 0.25)
        second = self.store.reserve_budget('run-limit', '0x00100001', 0.1, 0.25)
        third = self.store.reserve_budget('run-limit', '0x00100002', 0.1, 0.25)
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertIsNone(third)
        self.store.release_reservation(first)
        self.store.release_reservation(second)

    def test_concurrent_workers_share_atomic_session_reservations(self):
        self.store.update_budget('key', 20.0, 0.5, 100.0, run_id='run-concurrent')
        def reserve(i):
            return self.store.reserve_budget(
                'run-concurrent', f'0x{i:08x}', 0.1, 0.5)
        with ThreadPoolExecutor(max_workers=12) as pool:
            tokens = list(pool.map(reserve, range(12)))
        actual = [token for token in tokens if token]
        self.assertEqual(len(actual), 5)
        with self.store.connect() as db:
            total = db.execute(
                'SELECT SUM(amount) FROM reservations WHERE run_id=?',
                ('run-concurrent',)).fetchone()[0]
        self.assertAlmostEqual(total, 0.5)

    def test_inflight_reservations_reduce_available_budget(self):
        self.store.update_budget('key', 10.0, 0.2, 100.0, run_id='run-c')
        token = self.store.reserve_budget('run-c', '0x00000001', 0.1, 0.2)
        self.assertTrue(token)
        # An additional $0.10 reservation would be possible before usage
        # changes, but not after $0.10 was charged to the account.
        allowed, spent, reserved = self.store.update_budget(
            'key', 10.1, 0.2, 100.0, run_id='run-c')
        self.assertFalse(allowed)
        self.assertAlmostEqual(spent, 0.1)
        self.assertAlmostEqual(reserved, 0.1)

    def test_cannot_change_limit_within_existing_run(self):
        self.store.update_budget('key', 0.0, 5.0, 10.0, run_id='run-fixed')
        with self.assertRaisesRegex(RuntimeError, 'Budget changed'):
            self.store.update_budget('key', 0.1, 10.0, 10.0, run_id='run-fixed')

    def test_cannot_reuse_run_id_with_different_api_key(self):
        self.store.update_budget('key-A', 3.0, 5.0, 10.0, run_id='run-fixed')
        with self.assertRaises(RuntimeError):
            self.store.update_budget('key-B', 3.1, 5.0, 10.0, run_id='run-fixed')


if __name__ == '__main__':
    unittest.main()
