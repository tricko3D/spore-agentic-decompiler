"""Offline regression tests for the layered React dashboard backend."""
from __future__ import annotations
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dashboard import knowledge,consolidation
from dashboard.server import serve

class DashboardV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.repo=Path(self.tmp.name)
        p=knowledge.path_for(self.repo)
        p.parent.mkdir(parents=True)
        with sqlite3.connect(p) as db:
            db.executescript("""
              PRAGMA foreign_keys=ON;
              CREATE TABLE findings(
                id INTEGER PRIMARY KEY,va TEXT,pass_number INTEGER,status TEXT,
                verified_match INTEGER,title TEXT,summary TEXT,details TEXT,
                evidence_path TEXT,source_hash TEXT,created_utc TEXT);
              CREATE VIRTUAL TABLE findings_fts USING fts5(title,summary,details);
              CREATE TABLE embedding_jobs(finding_id INTEGER PRIMARY KEY);
              CREATE TABLE compiler_techniques(
                id INTEGER PRIMARY KEY,signature TEXT,title TEXT,category TEXT,
                problem TEXT,conditions TEXT,intervention TEXT,observed_effect TEXT,
                evidence_level TEXT,created_utc TEXT,updated_utc TEXT);
              CREATE VIRTUAL TABLE compiler_techniques_fts USING fts5(
                title,category,problem,conditions,intervention,observed_effect);
              CREATE TABLE compiler_technique_links(
                technique_id INTEGER,finding_id INTEGER,source_hash TEXT,
                source_status TEXT,evidence_quote TEXT,observation TEXT,created_utc TEXT);
              CREATE TABLE finding_experiments(
                finding_id INTEGER,ordinal INTEGER,flags TEXT,size_compiled INTEGER,
                size_retail INTEGER,diff_bytes INTEGER,result TEXT);
              CREATE TABLE technique_queue(
                finding_id INTEGER PRIMARY KEY,source_hash TEXT,state TEXT,
                failures INTEGER,error TEXT,updated_utc TEXT,next_retry_utc TEXT);
              CREATE TABLE technique_spend(day TEXT,cost_usd REAL);
            """)
            db.execute("""INSERT INTO findings VALUES (
                1,'0x00e61b90',2,'FAIL',0,'EAX calling convention',
                'Retail uses EAX register calling convention',
                'MSVC compiled 532/499 bytes; compiler register ABI differs',
                'work/scratch/evidence/0x00e61b90','hash-A','2026-11-18T00:00:00')""")
            db.execute("""INSERT INTO findings VALUES (
                2,'0x00d40930',1,'MATCH',1,'Stack layout',
                'Byte exact stack layout recovered',
                'MSVC x86 exact checker verified','work/scratch/evidence/0x00d40930',
                'hash-B','2026-11-18T00:01:00')""")
            for i in (1,2):
                db.execute("""INSERT INTO findings_fts(rowid,title,summary,details)
                    SELECT id,title,summary,details FROM findings WHERE id=?""",(i,))
            db.execute("""INSERT INTO compiler_techniques VALUES
                (1,'abc','EAX ABI mismatch','calling_convention',
                 'Retail passes EAX instead of stack','MSVC x86','not established',
                 '532 bytes versus 499 bytes','observed','2026-11-18T00:00:00','2026-11-18T00:00:00')""")
            db.execute("""INSERT INTO compiler_techniques_fts VALUES
                ('EAX ABI mismatch','calling_convention','Retail passes EAX instead of stack',
                'MSVC x86','not established','532 bytes versus 499 bytes')""")
            db.execute("""INSERT INTO compiler_technique_links VALUES
                (1,1,'hash-A','FAIL','Retail uses EAX register calling convention',
                'Register ABI still unresolved','2026-11-18T00:00:00')""")
            db.execute("""INSERT INTO compiler_technique_links VALUES
                (1,2,'STALE','MATCH','Wrong historical claim',
                'Must not appear because source changed','2026-11-18T00:00:00')""")
            db.execute("""INSERT INTO finding_experiments VALUES
                (1,2,'/O2 /MD',532,499,NULL,'NO MATCH')""")
            db.execute("""INSERT INTO technique_queue VALUES
                (1,'hash-A','done',0,'','2026-11-18T00:00:00',NULL)""")
            db.commit()
    def tearDown(self):
        self.tmp.cleanup()

    def test_knowledge_counts_queue_and_categories(self):
        data=knowledge.overview(self.repo)
        self.assertTrue(data["installed"])
        self.assertEqual(data["findings"],2)
        self.assertEqual(data["verified_matches"],1)
        self.assertEqual(data["techniques"],1)
        self.assertEqual(data["categories"][0]["category"],"calling_convention")
        self.assertEqual(data["queue"]["done"],1)

    def test_search_techniques_and_findings_without_sql_injection(self):
        d=knowledge.techniques(self.repo,search="EAX")
        self.assertEqual(d["total"],1)
        self.assertEqual(d["items"][0]["source_count"],1)
        self.assertEqual(knowledge.techniques(self.repo,search="!@#$%")["total"],0)
        self.assertEqual(knowledge.findings(self.repo,search="0x00e61b90")["total"],1)
        self.assertEqual(knowledge.findings(self.repo,search="byte exact")["total"],1)
        self.assertEqual(knowledge.findings(self.repo,status="MATCH")["total"],1)
        with self.assertRaises(ValueError):
            knowledge.findings(self.repo,status="MISSING")

    def test_click_through_and_stale_evidence_suppression(self):
        d=knowledge.technique(self.repo,1)
        self.assertEqual(len(d["sources"]),1)
        self.assertEqual(d["sources"][0]["finding_id"],1)
        graph=knowledge.graph(self.repo)
        self.assertEqual(len(graph["links"]),1)
        finding=knowledge.finding(self.repo,1)
        self.assertEqual(len(finding["experiments"]),1)
        self.assertEqual(finding["techniques"][0]["id"],1)
        matched=knowledge.finding(self.repo,2)
        self.assertEqual(matched["techniques"],[])

    def test_annotations_are_separate_from_compiler_evidence(self):
        result=knowledge.annotate(self.repo,1,"Try call-site changes",True)
        self.assertEqual(result["note"],"Try call-site changes")
        self.assertTrue(result["pinned"])
        latest=knowledge.technique(self.repo,1)
        self.assertEqual(latest["annotation"]["note"],"Try call-site changes")
        self.assertEqual(latest["evidence_level"],"observed")
        self.assertEqual(latest["problem"],"Retail passes EAX instead of stack")
        with self.assertRaises(ValueError):
            knowledge.annotate(self.repo,1,"x"*1501,False)
        with self.assertRaises(ValueError):
            knowledge.annotate(self.repo,999,"note",True)

    def test_all_getters_leave_sqlite_unmodified(self):
        file=knowledge.path_for(self.repo)
        before=file.stat().st_size
        knowledge.techniques(self.repo)
        knowledge.findings(self.repo)
        knowledge.finding(self.repo,1)
        knowledge.graph(self.repo)
        self.assertEqual(file.stat().st_size,before)

    def test_settings_are_validated_and_live_reloadable(self):
        result=consolidation.save_settings(self.repo,{"daily_usd":0.17,"batch":3,"interval":180})
        self.assertEqual(result["daily_usd"],.17)
        self.assertEqual(consolidation.settings(self.repo)["daily_usd"],.17)
        self.assertEqual(knowledge.path_for(self.repo).parent.joinpath(
            "consolidator-settings.json").stat().st_mode & 0o777,0o600)
        for data in ({"daily_usd":-1,"batch":3,"interval":180},
                     {"daily_usd":100,"batch":3,"interval":180},
                     {"daily_usd":0.25,"batch":0,"interval":180}):
            with self.assertRaises(ValueError):
                consolidation.save_settings(self.repo,data)

        path=ROOT/"engine/spore_provider_fix/work/scratch/spore_technique_consolidator.py"
        spec=importlib.util.spec_from_file_location("sad_v2_technique",path)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        actual=module.runtime_settings(self.repo,0.25,2,180)
        self.assertEqual(actual,{"daily_usd":0.17,"batch":3,"interval":180})

    def test_systemd_actions_have_no_user_controlled_command(self):
        fake_home=self.repo/"home"
        unit=fake_home/".config/systemd/user"/consolidation.UNIT
        unit.parent.mkdir(parents=True)
        unit.write_text("[Service]\\nType=simple\\n")
        with mock.patch.object(consolidation.Path,"home",return_value=fake_home):
            with mock.patch.object(consolidation,"_systemctl",return_value=(0,"active")) as invoke:
                consolidation.action(self.repo,"start")
                self.assertTrue(any(call.args[0]=="start" for call in invoke.call_args_list))
        with self.assertRaises(ValueError):
            consolidation.action(self.repo,"restart; rm -rf /")

    def test_server_imports_new_api_and_old_ui_remains_available(self):
        src=(ROOT/"dashboard/server.py").read_text(encoding="utf-8")
        self.assertIn('route=="/legacy"',src)
        self.assertIn('route=="/api/knowledge/graph"',src)
        self.assertIn('route=="/api/knowledge/annotation"',src)
        self.assertIn("X-Spore-Token",src)
        self.assertIn("localhost",src)
        self.assertIsNotNone(serve)

if __name__=="__main__":
    unittest.main()
