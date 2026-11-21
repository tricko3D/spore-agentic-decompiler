#!/usr/bin/env python3
"""Report verified successes from the current SAD parallel experiment."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=str(Path.home() / "spore-decomp"))
    ap.add_argument("--run-id", help="specific run ID, defaults to latest run")
    args = ap.parse_args()
    path = Path(args.repo).expanduser() / "work/scratch/codex-pass1/spore.sqlite3"
    if not path.is_file():
        ap.error("SAD SQLite database not found: " + str(path))
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        if args.run_id:
            run = db.execute("SELECT * FROM runs WHERE run_id=?", (args.run_id,)).fetchone()
        else:
            run = db.execute("SELECT * FROM runs ORDER BY started_utc DESC LIMIT 1").fetchone()
        if run is None:
            ap.error("No matching run found")
        started = datetime.fromisoformat(run["started_utc"])
        now = datetime.now(timezone.utc)
        duration = (now - started).total_seconds()/3600
        if run["finished_utc"]:
            duration = (datetime.fromisoformat(run["finished_utc"])-started).total_seconds()/3600
        rows = db.execute(
            "SELECT status,COUNT(*) n,COUNT(DISTINCT va) functions "
            "FROM attempts WHERE run_id=? GROUP BY status", (run["run_id"],)
        ).fetchall()
        counts = {r["status"]: r["n"] for r in rows}
        unique = db.execute("SELECT COUNT(DISTINCT va) FROM attempts WHERE run_id=?",
                            (run["run_id"],)).fetchone()[0]
        total = db.execute("SELECT COUNT(*) FROM assignments WHERE run_id=?",
                           (run["run_id"],)).fetchone()[0]
        active = db.execute("SELECT COUNT(*) FROM claims WHERE run_id=?",
                            (run["run_id"],)).fetchone()[0]
    matches = counts.get("MATCH", 0)
    equiv = counts.get("EQUIV", 0)
    print(f"Run: {run['run_id']} | {duration:.2f} hours elapsed"
          f" | workers {run['workers']} | active claims {active}")
    print(f"Assigned {total} functions | attempted {unique} distinct functions")
    print(f"NEW BYTE-EXACT MATCH: {matches}")
    print(f"NEW PROVEN EQUIVALENT: {equiv}")
    print("Other outcomes:", ", ".join(
        f"{s}={n}" for s, n in sorted(counts.items()) if s not in ("MATCH","EQUIV")) or "none")
    print(f"Verified matches/hour: {(matches/duration if duration > 0 else 0):.2f}")
    print(f"Verified matches + equivalents/hour: {((matches+equiv)/duration if duration>0 else 0):.2f}")
    print("Run completed:", "yes" if run["finished_utc"] else "not yet")

if __name__ == "__main__":
    main()
