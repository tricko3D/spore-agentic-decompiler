"""Regression coverage for upgrading an existing SQLite database during setup."""
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
ENGINE=ROOT/'engine'


class SchemaMigrationTests(unittest.TestCase):
    def test_checker_already_installed_still_applies_schema_migration(self):
        with tempfile.TemporaryDirectory() as temp:
            repo=Path(temp)
            (repo/'work/scratch').mkdir(parents=True)
            (repo/'tools/contrib').mkdir(parents=True)
            (repo/'work/scratch/codex-pass1').mkdir(parents=True)
            shutil.copyfile(ENGINE/'install_spore_sqlite.py',repo/'install_spore_sqlite.py')
            shutil.copyfile(ENGINE/'spore_provider_fix/work/scratch/spore_sqlite_store.py',
                            repo/'work/scratch/spore_sqlite_store.py')
            (repo/'work/scratch/spore_first_pass_sqlite.py').write_text('# worker installed\n')
            (repo/'tools/contrib/check.py').write_text(
                '# SPGONZAGO_SQLITE_CHECK_LOCK_V1\n')
            db_path=repo/'work/scratch/codex-pass1/spore.sqlite3'
            with sqlite3.connect(db_path) as db:
                db.execute('CREATE TABLE inference_slots (id TEXT PRIMARY KEY,run_id TEXT,va TEXT,created_utc TEXT)')
                db.execute("INSERT INTO inference_slots VALUES('old','prior-run','0x11223344','now')")
                db.execute('CREATE TABLE attempts (id INTEGER PRIMARY KEY,va TEXT,status TEXT)')
                db.execute("INSERT INTO attempts(id,va,status) VALUES(1,'0x11223344','MATCH')")
            result=subprocess.run([sys.executable,'install_spore_sqlite.py'],cwd=repo,
                                  text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
            self.assertIn('SQLite schema up to date',result.stdout)
            self.assertIn('Checker lock already installed',result.stdout)
            with sqlite3.connect(db_path) as db:
                columns={x[1] for x in db.execute('PRAGMA table_info(inference_slots)')}
                self.assertIn('verified',columns)
                self.assertEqual(db.execute("SELECT verified FROM inference_slots WHERE id='old'").fetchone()[0],0)
                self.assertEqual(db.execute('SELECT status FROM attempts WHERE id=1').fetchone()[0],'MATCH')
                self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE name='run_delays'").fetchone())


if __name__=='__main__':unittest.main()
