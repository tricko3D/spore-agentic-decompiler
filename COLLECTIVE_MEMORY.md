# SAD Collective Memory, dev branch

An optional shared knowledge base for the 32-agent SAD fleet, intended to reduce
repeated investigations without limiting reasoning steps. It holds structured
evidence about compiler flags, ABI problems, failed experiments and verified
byte-exact matches, and retrieves relevant prior cases via lexical and semantic
search.

## Storage and processes

| Item | Location in Spore checkout | Responsibility |
| --- | --- | --- |
| Operational database | work/scratch/codex-pass1/spore.sqlite3 | Budgets, workers, claims, valid attempts, unaffected |
| Investigation evidence | work/scratch/codex-pass1/investigations/<VA>/ | Original assembly, first C++, latest C++, source diff, checker experiments, curated notes |
| Collective Memory DB | work/scratch/codex-pass1/memory/knowledge.sqlite3 | Per-pass findings, FTS5, sqlite-vec vectors and embedding jobs |
| Cached model weights | work/scratch/codex-pass1/memory/models/ | Local ONNX model cache, retained on disk |
| Local service socket | work/scratch/codex-pass1/memory/memory.sock | One persistent embedder shared by all workers |
| Private Python environment | work/scratch/codex-pass1/memory/.venv/ | FastEmbed and sqlite-vec, separate from Spore and SAD Python |

Model: BAAI/bge-small-en-v1.5, 384-dimensional vectors, quantized ONNX
via fastembed==0.9.1. Search vectors are indexed in sqlite-vec==0.1.9.
The daemon uses CPU and two threads by default, to avoid competing with
your GPU. Model download happens once at installation; running the model uses
local_files_only=True. No embedding API, model-hosting subscription, PostgreSQL
or Docker is required.

The runner uses normal Python stdlib SQLite for very short inserts. A separate
service batches new embeddings, keeps the model loaded in memory and answers
requests over a local Unix socket, not a network listener. If that process is
offline, searches automatically use the FTS5 index. The vector extension is
only needed by the service process.

## Installation (when the 16 running workers have finished)

In the SAD application repository:

~~~bash
cd ~/spore-agentic-decompiler
git fetch origin dev
git switch dev
bash install_collective_memory.sh --repo ~/spore-decomp
~~~

The installer runs existing SAD setup, which refuses to replace runner scripts
while they are active. It creates the independent database, reads and imports
eligible prior attempts from operational SQLite in read-only mode, creates a
private environment, installs pinned dependencies, downloads and caches model
weights and launches the local embedding service. It never issues paid model
requests. There is no auto-launch of Spore workers.

To verify a lexical-only installation without pip/model downloads:

~~~bash
bash install_collective_memory.sh --repo ~/spore-decomp --no-model
~~~

To cache the embedding model without starting the service:

~~~bash
bash install_collective_memory.sh --repo ~/spore-decomp --no-start
~~~

The installer can be re-run, and retains existing knowledge. Run from WSL's
Linux filesystem (not /mnt/c) for Unix socket permissions and SQLite I/O.

## Improved evidence quality (v2)

Findings are not just closing notes. The importer parses completed actual
check.py tool calls in each historical JSONL attempt, capturing the precise
invoked command, compiler flags, compiled size, retail size, differences where
the tool actually reported a count, and diagnostic result. Reading check.py
source is never counted as a checker run. Checks with no reported byte-diff
count retain an unknown value, rather than an invented measurement.

The summary normalizer removes repeated historical notes. Results now include
up to three recent checker experiments and prefer distinct function addresses.
An independently proven byte match gets a relevance boost, but never forces
an unrelated verified function into a query. Use --verified-only to restrict
results to relevant verified matches. Matched functions can also contribute a
small excerpt of their existing locally verified C++ source for technical
retrieval.

During new investigations, actual checker events additionally produce a
best-effort post-check C++ snapshot and source delta in the per-function
candidates/ and candidate-diffs/ folders. These snapshots are captured when
the runner observes the tool result, potentially after a subsequent edit.
They are supporting evidence, **not proof** that an edit caused a reduction
in mismatching bytes.

Re-running the installer migrates the independent knowledge schema, imports
historical checker logs again, and restarts only an outdated Collective
Memory daemon after validating its PID and command line. It never kills SAD
workers or modifies operational attempt history. Source observations are
re-indexed locally by the daemon. The memory model does not need downloading
again when its cache is intact.


~~~bash
cd ~/spore-agentic-decompiler
git pull --ff-only origin dev
bash install_collective_memory.sh --repo ~/spore-decomp

cd ~/spore-decomp
.venv/bin/python work/scratch/spore_collective_memory.py --repo . query \
  --text "EAX calling convention register allocation stack cleanup" --top 4
.venv/bin/python work/scratch/spore_collective_memory.py --repo . query \
  --text "EAX calling convention register allocation stack cleanup" \
  --top 4 --verified-only
~~~

## Using Collective Memory

From within ~/spore-decomp:

~~~bash
# Health, stored findings, verified results and indexing backlog
.venv/bin/python work/scratch/spore_collective_memory.py --repo . status

# Exact term plus vector search when service is online; FTS fallback otherwise
.venv/bin/python work/scratch/spore_collective_memory.py --repo . query \
  --text "EAX register allocation caller stack cleanup" --top 4

# Read-only historical import, safe to repeat
.venv/bin/python work/scratch/spore_collective_memory.py --repo . backfill

# Start one server manually after reboot, if needed
work/scratch/codex-pass1/memory/.venv/bin/python \
  work/scratch/spore_collective_memory.py --repo . serve
~~~

Service PID and logs are at memory/service.pid and memory/service.log.
Agent prompts include the exact local query command, so OpenCode shell tools
need no new permissions or remote connections. For continuation passes,
SAD also performs a small initial retrieval based on the previous diagnosis.
Each result is bounded in size, and includes a verified status and a
reference to on-disk evidence. No huge histories are pasted into context.

## Trust and ingestion

A normal agent investigates and tests its assigned function. SAD's
independent checker decides whether byte bytes matched, and the normal
operational SQLite record is written first. Only then is a concise finding
inserted into Collective Memory. A claimed MATCH from agent prose is not
trusted. Each stored finding has its status, summary, evidence reference,
source hash and verification bit.

Historical backfill reads existing attempts in read-only mode and excludes
attempts invalidated in attempt_reviews. It only promotes historical MATCH
entries when the recorded note explicitly confirms independent verification.
Unverified historical claims are not imported as verified matches.

Results from unsuccessful attempts are valuable as negative knowledge and
rank below verified byte-exact matches. A retrieval match is an investigative
hint, not an authorization to edit unrelated source or trust model text.

Embeddings are generated locally and stored in the separate SQLite database.
FTS5 term search and sqlite-vec nearest-neighbor retrieval use rank fusion.
This keeps the source of evidence linked and allows later re-embedding without
touching worker budgets, runs, claims or Spore source.

## Compatibility and limitations

- Optional by design: if knowledge.sqlite3 does not exist, SAD behaves as before.
- Model/service errors do not stop inference or consume an attempt.
- One SQLite writer at a time is sufficient for the small, short per-pass writes.
  WAL permits concurrent readers from the worker fleet.
- Embeddings represent compact technical findings, not raw binary data. Exact
  compiler flags, register names and addresses remain searchable via FTS5.
- The initial archive is only as detailed as existing attempt notes. Newly
  created evidence provides more useful source diffs and experiments.
- Loading the real ONNX weights and running the persistent service are not
  exercised in CI, because CI tests must not download a model.
- The dev branch is experimental. It does not modify running main workers.

## Tests

~~~bash
python3 -m unittest discover -s tests -p 'test_collective_memory.py' -v
python3 -m unittest discover -s tests -p 'test_investigation_evidence.py' -v
~~~

The dev-only CI workflow validates Python syntax, the installer, lexical
retrieval, SQLite import behavior, the Unix socket protocol, and real
sqlite-vec indexing with deterministic in-test 384D vectors.

Research references:
- https://qdrant.tech/documentation/embeddings/fastembed/
- https://qdrant.github.io/fastembed/
- https://github.com/asg017/sqlite-vec
- https://github.com/asg017/sqlite-vec/blob/main/examples/simple-python/demo.py
- https://www.sqlite.org/fts5.html

If the fleet later spreads across different machines, its retrieval boundary
can be kept while replacing the local DB with PostgreSQL/pgvector.
