#!/usr/bin/env python3
"""Offline removal of evaluation-function knowledge from a frozen experiment DB.

Run using the cloned memory virtualenv so sqlite-vec virtual tables can be
opened safely. Never run against a production knowledge database.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sqlite3
import sys


def prune(dbpath: Path, holdouts: tuple[str, ...]) -> dict:
    if not holdouts or any(not re.fullmatch(r"0x[0-9a-f]{8}",v) for v in holdouts):
        raise ValueError("Expected canonical, nonempty holdout addresses")
    dbpath = dbpath.resolve(strict=True)
    if dbpath.name != "knowledge.sqlite3" or "/seed/" not in str(dbpath):
        raise ValueError("Refusing to modify a knowledge DB outside an experiment seed")
    con = sqlite3.connect(dbpath)
    try:
        con.row_factory = sqlite3.Row
        virtual = [r["sql"] or "" for r in con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL")]
        if any(re.search(r"\bUSING\s+vec0\b",s,re.I) for s in virtual):
            try:
                import sqlite_vec
            except ImportError as exc:
                raise RuntimeError("sqlite-vec is needed to scrub holdout embeddings") from exc
            con.enable_load_extension(True)
            sqlite_vec.load(con)
            con.enable_load_extension(False)
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        for name in ("findings","finding_experiments","compiler_techniques",
                     "compiler_technique_links","findings_fts","compiler_techniques_fts"):
            if name not in tables:
                raise RuntimeError(f"Missing expected evidence schema: {name}")
        found = set()
        for item in con.execute("SELECT id,va,title,summary,details FROM findings"):
            if (item["va"].lower() in holdouts or
                    any(v in str(item[key]).lower()
                        for v in holdouts for key in ("title","summary","details"))):
                found.add(int(item["id"]))
        for item in con.execute("SELECT finding_id,command,result FROM finding_experiments"):
            if any(v in str(item[key]).lower() for v in holdouts for key in ("command","result")):
                found.add(int(item["finding_id"]))
        affected_techniques = set()
        for item in con.execute("SELECT id,title,problem,conditions,intervention,observed_effect "
                                "FROM compiler_techniques"):
            if any(v in str(item[key]).lower()
                   for v in holdouts for key in (
                           "title","problem","conditions","intervention","observed_effect")):
                affected_techniques.add(int(item["id"]))
        for item in con.execute("SELECT technique_id,finding_id,evidence_quote,observation "
                                "FROM compiler_technique_links"):
            if (item["finding_id"] in found or
                    any(v in str(item[key]).lower()
                        for v in holdouts for key in ("evidence_quote","observation"))):
                affected_techniques.add(int(item["technique_id"]))
                # If an unlinked finding's link exposes holdout evidence,
                # remove that finding rather than only hiding the link.
                if any(v in str(item[key]).lower()
                       for v in holdouts for key in ("evidence_quote","observation")):
                    found.add(int(item["finding_id"]))
        # PRAGMA foreign_keys on only after selecting links, before deleting,
        # to let FK actions enforce dependent-row consistency.
        con.execute("PRAGMA foreign_keys=ON")
        for key in sorted(affected_techniques):
            if "technique_vectors" in tables:
                con.execute("DELETE FROM technique_vectors WHERE rowid=?",(key,))
            if "technique_embedding_jobs" in tables:
                con.execute("DELETE FROM technique_embedding_jobs WHERE technique_id=?",(key,))
            con.execute("DELETE FROM compiler_technique_links WHERE technique_id=?",(key,))
            con.execute("DELETE FROM compiler_techniques_fts WHERE rowid=?",(key,))
            con.execute("DELETE FROM compiler_techniques WHERE id=?",(key,))
        for key in sorted(found):
            if "finding_vectors" in tables:
                con.execute("DELETE FROM finding_vectors WHERE rowid=?",(key,))
            if "embedding_jobs" in tables:
                con.execute("DELETE FROM embedding_jobs WHERE finding_id=?",(key,))
            con.execute("DELETE FROM compiler_technique_links WHERE finding_id=?",(key,))
            con.execute("DELETE FROM finding_experiments WHERE finding_id=?",(key,))
            con.execute("DELETE FROM findings_fts WHERE rowid=?",(key,))
            con.execute("DELETE FROM findings WHERE id=?",(key,))
        # Addresses cannot remain visible in *any* textual evidence returned
        # by normal find/technique query paths.
        for table,columns in (
            ("findings",("va","title","summary","details","evidence_path")),
            ("compiler_techniques",("title","problem","conditions","intervention","observed_effect")),
            ("finding_experiments",("command","result")),
            ("compiler_technique_links",("evidence_quote","observation"))):
            for row in con.execute("SELECT "+",".join(columns)+" FROM "+table):
                if any(v in str(row[col]).lower()
                       for v in holdouts for col in columns):
                    raise RuntimeError(f"Holdout identity still appears in {table}")
        con.commit()
        return {"removed_findings":len(found),
                "removed_techniques":len(affected_techniques),
                "excluded_addresses":list(holdouts)}
    finally:
        con.close()


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db",type=Path,required=True)
    p.add_argument("--holdout",action="append",required=True)
    a=p.parse_args(argv)
    print(json.dumps(prune(a.db,tuple(a.holdout))))


if __name__=="__main__":
    main()
