#!/usr/bin/env python3
"""SAD Collective Memory: independent, local, evidence-backed retrieval.

Operational SQLite is NEVER touched here. Workers insert text findings using
stdlib only; one optional local service indexes embeddings via FastEmbed and
sqlite-vec. Retrieval remains functional using FTS5 if that service is offline.
No API keys, model-provider calls or arbitrary file paths are accepted.
"""
from __future__ import annotations

# Running a script inside Spore's work/scratch normally inserts that directory
# at sys.path[0]. The checkout contains a "dis.py" utility, which shadows
# Python's own dis module. On Python 3.14 argparse -> inspect -> dis imports
# that utility and fails before our CLI can initialize. This CLI is standalone:
# it does not need sibling files on sys.path, so remove ONLY that script directory
# before importing the standard library. Module imports by the SAD runner are
# unaffected.
import os as _bootstrap_os
import sys as _bootstrap_sys
if __name__ == "__main__":
    _script_dir = _bootstrap_os.path.realpath(_bootstrap_os.path.dirname(__file__))
    _bootstrap_sys.path[:] = [
        item for item in _bootstrap_sys.path
        if _bootstrap_os.path.realpath(item or _bootstrap_os.getcwd()) != _script_dir
    ]

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import sqlite3
import struct
import sys
import threading
import time
# Load the sibling parser explicitly without restoring work/scratch to the
# import search path: upstream dis.py would shadow Python's stdlib dis module.
import importlib.util as _evidence_util
_evidence_spec = _evidence_util.spec_from_file_location(
    "sad_spore_memory_evidence", Path(__file__).with_name("spore_memory_evidence.py"))
evidence = _evidence_util.module_from_spec(_evidence_spec)
_evidence_spec.loader.exec_module(evidence)

MODEL = "BAAI/bge-small-en-v1.5"
DIM = 384
MAX_DOC = 1800
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{8}$")
WORDS = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}|[0-9]{2,}")
SCHEMA = 1


def paths(repo):
    root = Path(repo).expanduser().resolve()
    folder = root / "work/scratch/codex-pass1/memory"
    return root, folder, folder / "knowledge.sqlite3", folder / "memory.sock"


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connection(repo, *, create=False, vectors=False):
    _, folder, path, _ = paths(repo)
    if not create and not path.is_file():
        return None
    if create:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder.chmod(0o700)
    con = sqlite3.connect(path, timeout=3.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=3000")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    if vectors:
        try:
            import sqlite_vec
            con.enable_load_extension(True)
            try:
                sqlite_vec.load(con)
            finally:
                con.enable_load_extension(False)
        except (ImportError, AttributeError, sqlite3.Error) as exc:
            con.close()
            raise RuntimeError("sqlite-vec extension unavailable: " + str(exc)) from exc
    return con


def initialize(repo, *, vectors=False):
    con = connection(repo, create=True, vectors=vectors)
    try:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS findings (
                id INTEGER PRIMARY KEY,
                va TEXT NOT NULL,
                pass_number INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('MATCH','FAIL','DEFER','SKIP')),
                verified_match INTEGER NOT NULL CHECK (verified_match IN (0,1)),
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                details TEXT NOT NULL,
                evidence_path TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                created_utc TEXT NOT NULL,
                UNIQUE(va, pass_number)
            );
            CREATE INDEX IF NOT EXISTS findings_va ON findings(va);
            CREATE INDEX IF NOT EXISTS findings_verified ON findings(verified_match);
            CREATE VIRTUAL TABLE IF NOT EXISTS findings_fts
                USING fts5(title, summary, details, tokenize='unicode61');
            CREATE TABLE IF NOT EXISTS embedding_jobs (
                finding_id INTEGER PRIMARY KEY REFERENCES findings(id) ON DELETE CASCADE,
                changed_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS finding_experiments (
                finding_id INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                command TEXT NOT NULL,
                flags TEXT NOT NULL,
                size_compiled INTEGER,
                size_retail INTEGER,
                diff_bytes INTEGER,
                offsets TEXT NOT NULL,
                result TEXT NOT NULL,
                PRIMARY KEY(finding_id, ordinal)
            );
            CREATE INDEX IF NOT EXISTS finding_experiments_diff
              ON finding_experiments(diff_bytes);
        """)
        if vectors:
            con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS finding_vectors "
                        "USING vec0(embedding float[384])")
            row = con.execute("SELECT value FROM metadata WHERE key='embedding_model'").fetchone()
            if row and row[0] != MODEL:
                raise RuntimeError("Embedding model changed; explicit reindex migration required")
            con.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('embedding_model',?)", (MODEL,))
        con.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('schema_version',?)", (str(SCHEMA),))
        con.commit()
    finally:
        con.close()


def _clean(text, max_length=1200):
    return re.sub(r"\s+", " ", str(text or "")).strip()[:max_length]


def _within_repo(repo, raw):
    root = Path(repo).expanduser().resolve()
    try:
        candidate = Path(raw)
        candidate = (candidate if candidate.is_absolute() else root / candidate).resolve()
        if candidate.is_relative_to(root):
            return candidate
    except (OSError, ValueError, TypeError):
        pass
    return None


def ingest(repo, va, pass_number, status, *, note, checker, evidence_path="", log_path=None):
    """Append independent pass evidence, never an unverified model MATCH claim.

    Caller MUST supply status after its own independent checker verdict.
    Import is idempotent by function/pass and updates stale evidence safely.
    """
    if not ADDRESS.fullmatch(va) or type(pass_number) is not int or pass_number < 1:
        raise ValueError("Invalid function identity")
    if status not in ("MATCH", "FAIL", "DEFER", "SKIP"):
        raise ValueError("Invalid outcome")
    if not paths(repo)[2].is_file():
        return False  # optional install; old workflow remains fully functional
    safe = _within_repo(repo, evidence_path) if evidence_path else None
    kept = ""
    diff = ""
    if safe and safe.is_dir():
        for name, limit in (("keep.md", 1000), ("source-diff.patch", 600)):
            file = safe / name
            try:
                snippet = file.read_text(encoding="utf-8", errors="replace")[:limit]
            except OSError:
                snippet = ""
            if name == "keep.md":
                kept = snippet
            else:
                diff = snippet
    verdict = _clean(checker, 650)
    observation = evidence.concise_summary(note, checker, 900)
    experiments = evidence.parse_experiments(log_path, va, repo) if log_path else []
    brief = evidence.experiment_brief(experiments)
    title = f"{va} {status} " + (observation or verdict)[:180]
    # A duplicated note is not additional evidence. Emphasize real checker
    # commands, observed metrics and hypotheses, not the previous transcript.
    details = _clean(
        f"Outcome: {status}. Observation: {observation}. "
        f"Independent checker: {verdict}. "
        f"Actual checker experiments: {brief}. "
        f"Curated hypotheses: {kept}. Source delta: {diff}", MAX_DOC)
    summary = observation or verdict
    digest = hashlib.sha256((title + summary + details).encode("utf-8")).hexdigest()
    rel = str(safe.relative_to(Path(repo).expanduser().resolve())) if safe else ""
    con = connection(repo)
    try:
        with con:
            row = con.execute("SELECT id,source_hash FROM findings WHERE va=? AND pass_number=?",
                              (va, pass_number)).fetchone()
            if row and row["source_hash"] == digest:
                if experiments:
                    _save_experiments(con, row["id"], experiments)
                return False
            if row:
                key = row["id"]
                con.execute("""UPDATE findings
                    SET status=?,verified_match=?,title=?,summary=?,details=?,
                        evidence_path=?,source_hash=?,created_utc=?
                    WHERE id=?""",
                    (status, int(status == "MATCH"), title, summary, details,
                     rel, digest, utc(), key))
                con.execute("DELETE FROM findings_fts WHERE rowid=?", (key,))
            else:
                cur = con.execute("""INSERT INTO findings(
                    va,pass_number,status,verified_match,title,summary,details,
                    evidence_path,source_hash,created_utc)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (va, pass_number, status, int(status == "MATCH"),
                     title, summary, details, rel, digest, utc()))
                key = cur.lastrowid
            con.execute("INSERT INTO findings_fts(rowid,title,summary,details) "
                        "VALUES(?,?,?,?)", (key, title, summary, details))
            con.execute("INSERT OR REPLACE INTO embedding_jobs(finding_id,changed_utc) "
                        "VALUES(?,?)", (key, utc()))
            if experiments:
                _save_experiments(con, key, experiments)
        return True
    finally:
        con.close()



def _save_experiments(con, finding_id, experiments):
    """Replace only completed checker executions. No model-invented results."""
    con.execute("DELETE FROM finding_experiments WHERE finding_id=?", (finding_id,))
    con.executemany("""INSERT INTO finding_experiments (
        finding_id, ordinal, command, flags, size_compiled, size_retail,
        diff_bytes, offsets, result
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", [
        (finding_id, item["ordinal"], item["command"], item["flags"],
         item["size_compiled"], item["size_retail"], item["diff_bytes"],
         item["offsets"], item["result"])
        for item in experiments
    ])


def backfill(repo, limit=10000):
    """Read historical SQLite attempts without writing to operational state.

    Only the runner's independently checked MATCH notes are promoted to
    verified. Older unverified MATCH records are skipped. Invalidated passes
    are omitted. Repeated imports are idempotent.
    """
    operational = Path(repo).expanduser().resolve() / "work/scratch/codex-pass1/spore.sqlite3"
    if not operational.is_file():
        return {"imported": 0, "skipped": 0, "reason": "No operational database"}
    initialize(repo)
    src = sqlite3.connect(operational.as_uri() + "?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    imported = skipped = 0
    try:
        columns = {item["name"] for item in src.execute("PRAGMA table_info(attempts)")}
        log_column = "a.log_path" if "log_path" in columns else "NULL AS log_path"
        rows = src.execute(f"""SELECT a.va,a.pass_number,a.status,a.note,{log_column}
            FROM attempts a
            WHERE NOT EXISTS (SELECT 1 FROM attempt_reviews v
                              WHERE v.attempt_id=a.id)
            ORDER BY a.id LIMIT ?""", (max(1, min(100000, limit)),))
        for row in rows:
            va, status = row["va"], row["status"]
            note = row["note"] or ""
            if status == "MATCH" and "independently byte-exact" not in note.lower():
                skipped += 1
                continue
            if not isinstance(va, str) or not ADDRESS.fullmatch(va):
                skipped += 1
                continue
            if status not in ("MATCH", "FAIL", "DEFER", "SKIP"):
                skipped += 1
                continue
            checker = note.rsplit("| checker:", 1)[-1] if "| checker:" in note else note
            evidence = (Path("work/scratch/codex-pass1/investigations") / va).as_posix()
            try:
                imported += int(ingest(repo, va, int(row["pass_number"]), status,
                                       note=note, checker=checker, evidence_path=evidence,
                                       log_path=row["log_path"]))
            except (sqlite3.Error, OSError, ValueError):
                skipped += 1
        return {"imported": imported, "skipped": skipped}
    finally:
        src.close()


def _candidate_rows(con, query, limit=40):
    terms = list(dict.fromkeys(WORDS.findall(query.lower())))[:16]
    if not terms:
        return []
    expression = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
    try:
        return con.execute("""SELECT f.id, bm25(findings_fts) AS rank
            FROM findings_fts JOIN findings f ON f.id=findings_fts.rowid
            WHERE findings_fts MATCH ? ORDER BY rank LIMIT ?""",
                           (expression, min(100, max(1, limit)))).fetchall()
    except sqlite3.OperationalError:
        return []


def _combine(con, query, *, vectors=None, limit=4, verified_only=False):
    """Fuse literal and semantic relevance; verified evidence wins close ties.

    Deduplicate passes from the same function to expose distinct approaches.
    Similarity is not proof: unrelated MATCH results should never be forced
    into results when the query finds only relevant failed experiments.
    """
    score = {}
    for order, row in enumerate(_candidate_rows(con, query)):
        score[row["id"]] = score.get(row["id"], 0) + 1.0 / (20 + order)
    if vectors:
        vec = struct.pack("<384f", *vectors)
        try:
            nearest = con.execute(
                "SELECT rowid,distance FROM finding_vectors "
                "WHERE embedding MATCH ? AND k = 40 ORDER BY distance",
                (vec,)).fetchall()
            for order, row in enumerate(nearest):
                score[row["rowid"]] = score.get(row["rowid"], 0) + 1.0 / (20 + order)
        except sqlite3.Error:
            pass
    if not score:
        return []
    ids = sorted(score)
    rows = con.execute("SELECT * FROM findings WHERE id IN (" +
                       ",".join("?" for _ in ids) + ")", ids).fetchall()
    for item in rows:
        if item["verified_match"]:
            score[item["id"]] *= 1.40
        if item["va"].lower() in query.lower():
            score[item["id"]] *= 1.20
    ordered = sorted(rows, key=lambda item: (-score[item["id"]],
                                            -item["verified_match"], -item["id"]))
    distinct = []
    seen_va = set()
    for item in ordered:
        if verified_only and not item["verified_match"]:
            continue
        if item["va"] in seen_va:
            continue
        seen_va.add(item["va"])
        distinct.append(item)
        if len(distinct) >= max(1, min(10, limit)):
            break
    result = []
    for row in distinct:
        attempts = con.execute("""SELECT ordinal,flags,size_compiled,size_retail,
                diff_bytes,result FROM finding_experiments
                WHERE finding_id=? ORDER BY ordinal DESC LIMIT 3""",
                               (row["id"],)).fetchall()
        history = []
        for e in reversed(attempts):
            facts = []
            if e["flags"]:
                facts.append("flags " + e["flags"])
            if e["size_compiled"] is not None and e["size_retail"] is not None:
                facts.append(f"size {e['size_compiled']}/{e['size_retail']}")
            if e["diff_bytes"] is not None:
                facts.append(f"{e['diff_bytes']} different bytes")
            if not facts:
                facts.append(e["result"][:110])
            history.append(f"#{e['ordinal']}: " + ", ".join(facts))
        result.append({
            "va": row["va"], "pass": row["pass_number"],
            "status": row["status"], "verified_match": bool(row["verified_match"]),
            "summary": row["summary"][:450], "experiments": history,
            "evidence_path": row["evidence_path"],
            "score": round(score[row["id"]], 6),
        })
    return result

def lexical(repo, query, limit=4, verified_only=False):
    con = connection(repo)
    if not con:
        return []
    try:
        return _combine(con, query, limit=limit, verified_only=verified_only)
    finally:
        con.close()


def request(repo, payload, *, timeout=3):
    _, _, _, sock = paths(repo)
    if not sock.is_socket():
        return None
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    if len(data) > 16384:
        raise ValueError("Memory request exceeds limit")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(sock))
            client.sendall(data)
            chunks = bytearray()
            while b"\n" not in chunks and len(chunks) <= 150000:
                piece = client.recv(8192)
                if not piece:
                    break
                chunks.extend(piece)
        if b"\n" not in chunks:
            return None
        reply = json.loads(chunks.split(b"\n", 1)[0])
        if isinstance(reply, dict) and not reply.get("error"):
            return reply
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return None


def query(repo, text, limit=4, verified_only=False):
    text = _clean(text, 1800)
    if not text:
        return []
    remote = request(repo, {"method": "query", "query": text, "top": limit,
                            "verified_only": bool(verified_only)})
    if remote is not None:
        return remote.get("results", [])
    return lexical(repo, text, limit, verified_only=verified_only)


def print_findings(rows, *, max_chars=2000):
    output = []
    for r in rows:
        label = ("VERIFIED MATCH" if r["verified_match"] else "UNMATCHED EXPERIMENT")
        experiments = r.get("experiments") or []
        proof = ("\n  Checks: " + " | ".join(experiments)[:400]) if experiments else ""
        output.append(f"[{label}] {r['va']} pass {r['pass']}:\n"
                      f"  {r['summary'][:330]}{proof}\n"
                      f"  evidence: {r['evidence_path']}")
    return ("\n\n".join(output) or "No relevant shared findings yet.")[:max_chars]


class LocalEmbeddings:
    """Loaded exactly once in the memory daemon, CPU-only by default."""
    def __init__(self, folder, *, offline=True):
        from fastembed import TextEmbedding
        self.model = TextEmbedding(model_name=MODEL, cache_dir=str(folder / "models"),
                                   threads=2, cuda=False, local_files_only=offline)
        self.lock = threading.Lock()

    def encode(self, texts):
        with self.lock:
            rows = list(self.model.embed(list(texts), batch_size=8, parallel=None))
        result = []
        for item in rows:
            numbers = [float(n) for n in item]
            if len(numbers) != DIM:
                raise RuntimeError("Embedding dimension mismatch")
            result.append(numbers)
        return result


class Service:
    def __init__(self, repo, *, offline=True):
        self.repo = Path(repo).resolve()
        self.folder = paths(repo)[1]
        self.embedder = LocalEmbeddings(self.folder, offline=offline)
        initialize(repo, vectors=True)
        self.stop = threading.Event()

    def index_once(self, batch=8):
        con = connection(self.repo, vectors=True)
        try:
            rows = con.execute("""SELECT f.id,f.title,f.summary,f.details
              FROM embedding_jobs j JOIN findings f ON f.id=j.finding_id
              ORDER BY f.id LIMIT ?""", (batch,)).fetchall()
            if not rows:
                return 0
            texts = [_clean(" " .join((r["title"], r["summary"], r["details"])),
                            MAX_DOC) for r in rows]
            vectors = self.embedder.encode(texts)
            with con:
                for r, vector in zip(rows, vectors):
                    key = r["id"]
                    con.execute("DELETE FROM finding_vectors WHERE rowid=?", (key,))
                    con.execute("INSERT INTO finding_vectors(rowid,embedding) VALUES(?,?)",
                                (key, struct.pack("<384f", *vector)))
                    con.execute("DELETE FROM embedding_jobs WHERE finding_id=?", (key,))
            return len(rows)
        finally:
            con.close()

    def search(self, text, limit, verified_only=False):
        vector = self.embedder.encode([text[:1800]])[0]
        con = connection(self.repo, vectors=True)
        try:
            return _combine(con, text, vectors=vector, limit=limit, verified_only=verified_only)
        finally:
            con.close()

    def background(self):
        while not self.stop.is_set():
            try:
                count = self.index_once()
            except Exception as exc:
                print("Memory indexing issue:", str(exc)[:220], file=sys.stderr, flush=True)
                count = 0
            self.stop.wait(0.1 if count else 3.0)


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline(16385)
        if not line or len(line) > 16384:
            return
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError("Expected object")
            method = req.get("method")
            if method == "query":
                text = str(req.get("query", ""))[:1800]
                top = max(1, min(10, int(req.get("top", 4))))
                result = {"results": self.server.service.search(
                    text, top, verified_only=bool(req.get("verified_only", False)))}
            elif method == "status":
                result = {"ok": True, "model": MODEL, "vector_dim": DIM}
            else:
                result = {"error": "Unknown method"}
        except (ValueError, TypeError, sqlite3.Error, RuntimeError) as exc:
            result = {"error": str(exc)[:200]}
        self.wfile.write((json.dumps(result) + "\n").encode("utf-8"))


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(repo):
    _, folder, _, sock = paths(repo)
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    # Refuse to steal a live service's socket.
    if sock.exists():
        if request(repo, {"method": "status"}, timeout=1) is not None:
            raise SystemExit("SAD memory service already running")
        sock.unlink()
    service = Service(repo, offline=True)
    server = UnixServer(str(sock), Handler)
    server.service = service
    os.chmod(sock, 0o600)
    thread = threading.Thread(target=service.background, daemon=True)
    thread.start()
    print("Collective Memory online, Unix socket:", sock, flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        service.stop.set()
        server.server_close()
        sock.unlink(missing_ok=True)


def stats(repo):
    con = connection(repo)
    if not con:
        return {"installed": False}
    try:
        info = con.execute("SELECT COUNT(*),SUM(verified_match) FROM findings").fetchone()
        pending = con.execute("SELECT COUNT(*) FROM embedding_jobs").fetchone()[0]
        service = request(repo, {"method": "status"}, timeout=0.5)
        return {"installed": True, "findings": info[0],
                "verified": info[1] or 0, "pending_embeddings": pending,
                "service_online": service is not None,
                "embedding_model": service.get("model") if service else None}
    finally:
        con.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="Spore checkout root, default cwd")
    command = parser.add_subparsers(dest="command", required=True)
    command.add_parser("init", help="Initialize only independent memory SQLite")
    command.add_parser("warmup", help="Download and cache the local embedding model")
    command.add_parser("serve", help="Start local persistent model and vector service")
    command.add_parser("status", help="Show service state and indexed findings")
    backfill_cmd = command.add_parser("backfill", help="Import prior independently checked attempts")
    backfill_cmd.add_argument("--limit", type=int, default=100000)
    query_cmd = command.add_parser("query", help="Retrieve reusable evidence from all functions")
    query_cmd.add_argument("--text", required=True)
    query_cmd.add_argument("--top", type=int, default=4)
    query_cmd.add_argument("--verified-only", action="store_true",
                           help="Retrieve only relevant independently verified matches")
    ingest_cmd = command.add_parser("ingest", help="Import an independently checked outcome")
    ingest_cmd.add_argument("--va", required=True)
    ingest_cmd.add_argument("--pass-number", type=int, required=True)
    ingest_cmd.add_argument("--status", required=True)
    ingest_cmd.add_argument("--note", default="")
    ingest_cmd.add_argument("--checker", default="")
    ingest_cmd.add_argument("--evidence-path", default="")
    ingest_cmd.add_argument("--log-path", default="")
    args = parser.parse_args(argv)
    if args.command == "init":
        initialize(args.repo)
        print(json.dumps(stats(args.repo)))
    elif args.command == "warmup":
        _, folder, _, _ = paths(args.repo)
        folder.mkdir(parents=True, exist_ok=True)
        embedder = LocalEmbeddings(folder, offline=False)
        embedder.encode(["ABI register allocation calling convention byte matching"])
        print("Local embedding weights cached at:", folder / "models")
    elif args.command == "serve":
        serve(args.repo)
    elif args.command == "status":
        print(json.dumps(stats(args.repo), indent=2))
    elif args.command == "query":
        print(print_findings(query(args.repo, args.text, args.top,
                                 verified_only=args.verified_only)))
    elif args.command == "backfill":
        print(json.dumps(backfill(args.repo, args.limit)))
    elif args.command == "ingest":
        result = ingest(args.repo, args.va, args.pass_number, args.status,
                        note=args.note, checker=args.checker, evidence_path=args.evidence_path,
                        log_path=args.log_path)
        print("Stored" if result else "Unchanged or not installed")


if __name__ == "__main__":
    main()
