"""Offline tests of OpenRouter key rotation and budget carryover."""
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

CLI_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(CLI_ROOT))
from spore_agentic import rebind_budget_key


class RotationTests(unittest.TestCase):
    def test_old_usage_survives_new_key_and_prior_state_remains(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)
            path=repo/'work/scratch/codex-pass1/spore.sqlite3'
            path.parent.mkdir(parents=True)
            old=hashlib.sha256(b'sk-or-old').hexdigest()[:20]
            new=hashlib.sha256(b'sk-or-new').hexdigest()[:20]
            with sqlite3.connect(path) as db:
                db.executescript("""
                    CREATE TABLE budget(singleton INTEGER PRIMARY KEY,key_fingerprint TEXT,
                     baseline_usage REAL,last_usage REAL,updated_utc TEXT);
                    CREATE TABLE attempts(id INTEGER PRIMARY KEY,va TEXT,status TEXT);
                    INSERT INTO attempts VALUES(17,'0x00dba3a0','DEFER');
                """)
                db.execute("INSERT INTO budget VALUES(1,?,100.0,103.1218,'now')",(old,))
            result=rebind_budget_key(repo,new,0.50)
            self.assertTrue(result['changed'])
            self.assertTrue(result['backup'].is_file())
            self.assertAlmostEqual(result['spent'],3.1218)
            with sqlite3.connect(path) as db:
                fingerprint,base,last=db.execute('SELECT key_fingerprint,baseline_usage,last_usage FROM budget').fetchone()
                self.assertEqual(fingerprint,new)
                self.assertAlmostEqual(last-base,3.1218)
                self.assertAlmostEqual(last,0.50)
                self.assertEqual(db.execute('SELECT status FROM attempts').fetchone()[0],'DEFER')
                self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_key_rotations').fetchone()[0],1)
                db.execute('UPDATE budget SET last_usage=1.00')
                spent=db.execute('SELECT last_usage-baseline_usage FROM budget').fetchone()[0]
                self.assertAlmostEqual(spent,3.6218)
            with sqlite3.connect(result['backup']) as db:
                self.assertEqual(db.execute('SELECT key_fingerprint FROM budget').fetchone()[0],old)
                self.assertEqual(db.execute('SELECT status FROM attempts').fetchone()[0],'DEFER')
            second=rebind_budget_key(repo,new,1.00)
            self.assertFalse(second['changed'])
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_key_rotations').fetchone()[0],1)


if __name__=='__main__':
    unittest.main()
