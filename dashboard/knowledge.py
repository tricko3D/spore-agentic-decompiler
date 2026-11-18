"""Read-focused, version-aware access to the independent Collective Memory.

Never opens the decompiler's operational DB and never performs model inference.
All search and detail methods use read-only SQLite connections. The only write
is a separate, auditable dashboard annotation table.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

WORDS = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}|[0-9]{2,}")
VA = re.compile(r"0x[0-9a-fA-F]{8}\Z")
CATEGORIES = frozenset((
    "calling_convention","register_allocation","stack_layout","compiler_flags",
    "optimization","control_flow","layout_types","templates","link_time","other"))
STATES = frozenset(("MATCH","FAIL","DEFER","SKIP"))
NOTE_LIMIT = 1500


def path_for(repo: Path) -> Path:
    return Path(repo) / "work/scratch/codex-pass1/memory/knowledge.sqlite3"


@contextmanager
def connection(repo: Path, *, writable=False):
    path = path_for(repo)
    if not path.is_file():
        raise FileNotFoundError("Collective Memory not initialized")
    uri = path.resolve().as_uri() + ("" if writable else "?mode=ro")
    db = sqlite3.connect(uri, uri=True, timeout=4)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=4000")
    db.execute("PRAGMA foreign_keys=ON")
    if not writable:
        db.execute("PRAGMA query_only=ON")
    try:
        yield db
    finally:
        db.close()


def _scalar(db, query, *args):
    return db.execute(query, args).fetchone()[0]


def _has(db, table):
    return bool(db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _bounded(text, limit=1200):
    return str(text or "")[:limit]


def _where_fts(query):
    words = list(dict.fromkeys(WORDS.findall(query.lower())))[:12]
    return " OR ".join('"' + term.replace('"','""') + '"' for term in words)


def _page(offset,limit):
    if type(offset) is not int or not 0 <= offset <= 100000:
        raise ValueError("Offset must be an integer from 0 to 100000")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Limit must be from 1 to 100")
    return offset,limit


def overview(repo):
    try:
        with connection(repo) as db:
            existing = _has(db,"compiler_techniques")
            result = {
                "installed": True,
                "findings": _scalar(db,"SELECT COUNT(*) FROM findings"),
                "verified_matches": _scalar(db,"SELECT COUNT(*) FROM findings WHERE verified_match=1"),
                "pending_embeddings": _scalar(db,"SELECT COUNT(*) FROM embedding_jobs"),
                "techniques": _scalar(db,"SELECT COUNT(*) FROM compiler_techniques") if existing else 0,
                "links": _scalar(db,"SELECT COUNT(*) FROM compiler_technique_links") if existing else 0,
                "queue": {},
                "categories": [],
                "errors": [],
                "recent_techniques": [],
                "daily_spend": 0,
            }
            if existing:
                result["categories"] = [dict(r) for r in db.execute(
                    "SELECT category,COUNT(*) AS count FROM compiler_techniques GROUP BY category ORDER BY count DESC")]
                result["recent_techniques"] = [
                    dict(r) for r in db.execute("""SELECT id,title,category,evidence_level,updated_utc
                        FROM compiler_techniques ORDER BY updated_utc DESC,id DESC LIMIT 6""")]
            if _has(db,"technique_queue"):
                result["queue"] = {r["state"]:r["count"] for r in db.execute(
                    "SELECT state,COUNT(*) AS count FROM technique_queue GROUP BY state")}
                columns = {r["name"] for r in db.execute("PRAGMA table_info(technique_queue)")}
                retry = "q.next_retry_utc" if "next_retry_utc" in columns else "NULL"
                result["errors"] = [dict(r) for r in db.execute(f"""SELECT f.va,q.finding_id,
                    q.state,q.error,q.failures,{retry} AS next_retry_utc
                    FROM technique_queue q JOIN findings f ON f.id=q.finding_id
                    WHERE q.error <> '' ORDER BY q.updated_utc DESC LIMIT 8""")]
            if _has(db,"technique_spend"):
                day = datetime.now(timezone.utc).date().isoformat()
                result["daily_spend"] = round(_scalar(db,
                    "SELECT COALESCE(SUM(cost_usd),0) FROM technique_spend WHERE day=?", day),6)
            return result
    except FileNotFoundError:
        return {"installed":False,"findings":0,"verified_matches":0,
                "pending_embeddings":0,"techniques":0,"links":0,
                "queue":{},"categories":[],"errors":[],"recent_techniques":[],
                "daily_spend":0}


def _annotation(db,technique_id):
    if not _has(db,"knowledge_annotations"):
        return {"note":"","pinned":False,"updated_utc":None}
    row=db.execute("SELECT note,pinned,updated_utc FROM knowledge_annotations WHERE technique_id=?",
                   (technique_id,)).fetchone()
    return {"note":row["note"],"pinned":bool(row["pinned"]),
            "updated_utc":row["updated_utc"]} if row else {
                "note":"","pinned":False,"updated_utc":None}


def techniques(repo, *, search="",category="",offset=0,limit=40):
    offset,limit=_page(offset,limit)
    if not isinstance(search,str) or len(search)>180:
        raise ValueError("Search too long")
    if category and category not in CATEGORIES:
        raise ValueError("Unknown compiler category")
    with connection(repo) as db:
        if not _has(db,"compiler_techniques"):
            return {"total":0,"items":[],"offset":offset}
        params=[]
        where=["EXISTS (SELECT 1 FROM compiler_technique_links l JOIN findings f ON f.id=l.finding_id "
               "WHERE l.technique_id=t.id AND l.source_hash=f.source_hash)"]
        if category:
            where.append("t.category=?");params.append(category)
        expr=_where_fts(search)
        if search.strip():
            if not expr:
                return {"total":0,"items":[],"offset":offset}
            where.append("t.id IN (SELECT rowid FROM compiler_techniques_fts WHERE compiler_techniques_fts MATCH ?)")
            params.append(expr)
        where_sql=" AND ".join(where)
        total=_scalar(db,"SELECT COUNT(*) FROM compiler_techniques t WHERE "+where_sql,*params)
        rows=db.execute("""SELECT t.id,t.title,t.category,t.problem,t.conditions,t.intervention,
                  t.observed_effect,t.evidence_level,t.updated_utc,
                  (SELECT COUNT(*) FROM compiler_technique_links l JOIN findings f ON f.id=l.finding_id
                    WHERE l.technique_id=t.id AND l.source_hash=f.source_hash) AS source_count
                  FROM compiler_techniques t WHERE """+where_sql+
                  " ORDER BY t.updated_utc DESC,t.id DESC LIMIT ? OFFSET ?",
                  (*params,limit,offset)).fetchall()
        return {"total":total,"offset":offset,"items":[dict(r) for r in rows]}


def technique(repo,ident):
    if type(ident) is not int or not 1<=ident<=100000000:
        raise ValueError("Invalid technique ID")
    with connection(repo) as db:
        row=db.execute("SELECT * FROM compiler_techniques WHERE id=?",(ident,)).fetchone()
        if not row:
            raise ValueError("Technique not found")
        links=[dict(x) for x in db.execute("""SELECT f.va,f.pass_number,f.status,
            f.verified_match,f.evidence_path,l.evidence_quote,l.observation,l.created_utc,
            f.id AS finding_id FROM compiler_technique_links l
            JOIN findings f ON f.id=l.finding_id
            WHERE l.technique_id=? AND l.source_hash=f.source_hash
            ORDER BY f.verified_match DESC,l.created_utc DESC LIMIT 100""",(ident,))]
        return {**dict(row),"sources":links,"annotation":_annotation(db,ident)}


def findings(repo, *, search="",status="",offset=0,limit=40):
    offset,limit=_page(offset,limit)
    if not isinstance(search,str) or len(search)>180:
        raise ValueError("Search too long")
    if status and status not in STATES:
        raise ValueError("Invalid finding status")
    with connection(repo) as db:
        where=["1=1"]; params=[]
        if status:
            where.append("f.status=?");params.append(status)
        if search.strip():
            if VA.fullmatch(search.strip()):
                where.append("f.va=?");params.append(search.strip().lower())
            else:
                expr=_where_fts(search)
                if not expr:return {"total":0,"offset":offset,"items":[]}
                where.append("f.id IN (SELECT rowid FROM findings_fts WHERE findings_fts MATCH ?)")
                params.append(expr)
        cond=" AND ".join(where)
        total=_scalar(db,"SELECT COUNT(*) FROM findings f WHERE "+cond,*params)
        rows=db.execute("""SELECT f.id,f.va,f.pass_number,f.status,f.verified_match,
          f.title,f.summary,f.created_utc,f.evidence_path,
          (SELECT COUNT(*) FROM compiler_technique_links l
           WHERE l.finding_id=f.id AND l.source_hash=f.source_hash) AS technique_count
          FROM findings f WHERE """+cond+" ORDER BY f.created_utc DESC,f.id DESC LIMIT ? OFFSET ?",
          (*params,limit,offset)).fetchall()
        return {"total":total,"offset":offset,"items":[dict(r) for r in rows]}


def finding(repo,ident):
    if type(ident) is not int or not 1<=ident<=1000000000:
        raise ValueError("Invalid finding ID")
    with connection(repo) as db:
        row=db.execute("SELECT * FROM findings WHERE id=?",(ident,)).fetchone()
        if not row:raise ValueError("Finding not found")
        checks=[dict(x) for x in db.execute("""SELECT ordinal,flags,size_compiled,
                    size_retail,diff_bytes,result FROM finding_experiments
                    WHERE finding_id=? ORDER BY ordinal LIMIT 40""",(ident,))]
        links=[dict(x) for x in db.execute("""SELECT t.id,t.title,t.category,
                    t.evidence_level,l.observation FROM compiler_technique_links l
                    JOIN compiler_techniques t ON t.id=l.technique_id
                    WHERE l.finding_id=? AND l.source_hash=?
                    ORDER BY t.id""",(ident,row["source_hash"]))]
        # Evidence paths are identifiers, not a browser-controlled file read.
        return {**dict(row),"experiments":checks,"techniques":links}


def graph(repo,limit=45):
    if type(limit) is not int or not 1<=limit<=80:raise ValueError("Invalid graph limit")
    with connection(repo) as db:
        if not _has(db,"compiler_techniques"):
            return {"techniques":[],"findings":[],"links":[]}
        nodes=[dict(x) for x in db.execute(
            "SELECT id,title,category,evidence_level FROM compiler_techniques ORDER BY updated_utc DESC,id DESC LIMIT ?",
            (limit,))]
        if not nodes:
            return {"techniques":[],"findings":[],"links":[]}
        ids=[x["id"] for x in nodes]
        ph=",".join("?" for _ in ids)
        links=[dict(x) for x in db.execute("""SELECT l.technique_id,f.id AS finding_id,
                      f.va,f.status,f.verified_match FROM compiler_technique_links l
                      JOIN findings f ON f.id=l.finding_id WHERE l.source_hash=f.source_hash
                      AND l.technique_id IN ("""+ph+") LIMIT 240",ids)]
        findings_list=list({x["finding_id"]:{"id":x["finding_id"],"va":x["va"],
                          "status":x["status"],"verified_match":x["verified_match"]}
                          for x in links}.values())
        return {"techniques":nodes,"findings":findings_list,
                "links":[{"technique_id":x["technique_id"],
                          "finding_id":x["finding_id"]} for x in links]}


def annotate(repo,ident,note,pinned):
    if type(ident) is not int or not 1<=ident<=100000000:raise ValueError("Invalid technique")
    if not isinstance(note,str) or len(note)>NOTE_LIMIT:raise ValueError("Note exceeds 1500 characters")
    if type(pinned) is not bool:raise ValueError("Invalid pinned value")
    with connection(repo,writable=True) as db:
        if not db.execute("SELECT 1 FROM compiler_techniques WHERE id=?",(ident,)).fetchone():
            raise ValueError("Technique not found")
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS knowledge_annotations (
                technique_id INTEGER PRIMARY KEY REFERENCES compiler_techniques(id)
                  ON DELETE CASCADE,
                note TEXT NOT NULL,pinned INTEGER NOT NULL CHECK(pinned IN (0,1)),
                updated_utc TEXT NOT NULL
            )""")
            db.execute("""INSERT INTO knowledge_annotations(technique_id,note,pinned,updated_utc)
                VALUES(?,?,?,?) ON CONFLICT(technique_id) DO UPDATE SET
                note=excluded.note,pinned=excluded.pinned,updated_utc=excluded.updated_utc""",
                (ident,note,1 if pinned else 0,
                 datetime.now(timezone.utc).isoformat(timespec="seconds")))
        return _annotation(db,ident)
