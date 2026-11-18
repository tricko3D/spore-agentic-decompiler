# SAD Compiler Technique Consolidator (experimental dev)

This is the optional second layer of Collective Memory. The existing system
records what happened to each function; the DeepSeek consolidator extracts
reusable compiler observations from those records. It does not solve functions,
edit C++, touch the operational SQLite database, or promote model claims into
byte-exact matches.

The configured model is the same one used by current SAD workers:
deepseek/deepseek-v4.1-flash-1017. It is pinned to Sail Research with
provider fallback disabled. Requests are small, non-streaming, JSON-only, and
use OpenRouter's per-token max-price limits. No new key is needed: it reads the
existing protected SAD API key from the environment or
~/.config/spore-agentic-decompiler/openrouter.key.

The local BAAI/bge-small-en-v1.5 model is only for embeddings and retrieval.
The technical analysis step is paid DeepSeek inference through OpenRouter.

## Architecture

1. The worker verifies its candidate with the independent checker, recording
   the usual attempt in operational SQLite.
2. spore_collective_memory.py stores the result in independent knowledge SQLite.
3. spore_technique_consolidator.py init queues new and changed findings.
   Historical findings are queued on first setup, with no paid requests.
4. An explicitly invoked run or serve command sends a brief, prioritized
   finding plus up to five potentially related techniques to DeepSeek.
5. Up to three structured technique proposals can be returned. Each must cite
   a verbatim extract from the actual finding; IDs, categories and citations
   are validated locally before storage.
6. Create adds a proposed compiler technique; extend adds another evidence
   link without replacing the existing technical description.
7. The local FastEmbed daemon embeds the new technique in a separate
   384-dimensional sqlite-vec index. FTS5 search works without the daemon.
8. Workers can retrieve consolidated techniques alongside original
   investigation histories, explicitly marked as observed or hypothetical,
   not verified compiler rules.

The new tables in work/scratch/codex-pass1/memory/knowledge.sqlite3 include
compiler_techniques, compiler_technique_links, compiler_techniques_fts,
technique_queue, technique_embedding_jobs, technique_spend and the
technique_vectors virtual table.

The operational Spore database, compiler checks and worker budget ledger are
not modified or shared. A file lock prevents duplicate concurrent consolidator
requests. Source-version changes invalidate old supporting evidence until
re-consolidated.

## Installation, no paid requests

From the SAD dev branch, when workers are idle for script replacement:

~~~bash
cd ~/spore-agentic-decompiler
git pull --ff-only origin dev
bash install_collective_memory.sh --repo ~/spore-decomp
~~~

The installer registers the new script, creates the queue and index, and
restarts the local memory daemon when its protocol changes. It never starts
paid consolidation, never sends a DeepSeek request, and reuses model weights
already downloaded.

## Free commands

~~~bash
cd ~/spore-decomp

.venv/bin/python work/scratch/spore_technique_consolidator.py \
  --repo . status

.venv/bin/python work/scratch/spore_technique_consolidator.py \
  --repo . preview --top 3

.venv/bin/python work/scratch/spore_technique_consolidator.py \
  --repo . search --text "EAX calling convention stack cleanup"

.venv/bin/python work/scratch/spore_collective_memory.py \
  --repo . techniques --text "EAX calling convention stack cleanup"
~~~

The preview shows the queued evidence that would be sent. It does not call
OpenRouter, reserve spending or require an API key.

## Explicitly enabling paid processing

One bounded batch:

~~~bash
cd ~/spore-decomp
.venv/bin/python work/scratch/spore_technique_consolidator.py \
  --repo . run --max 10 --daily-usd 0.25
~~~

The model is invoked serially, with one request per finding. Each response
may propose 0-3 techniques. The daily budget is an estimated local admission
cap dedicated to consolidation; each request reserves $0.01 until completion
and substitutes its actual reported provider cost when available. Failures
where billing is uncertain retain their reservation. This is deliberately
conservative, but not a provider-enforced hard guarantee. For a hard account
cap, configure the key or workspace credit limit with OpenRouter. The provider
also has per-million-token price ceilings of $0.50 input and $1.00 output,
which are maximum rates, not expected rates.

### Automatic consolidation, no manual batches

After installing the updated dev scripts, one explicit command enables
unattended paid technique extraction. You no longer have to run ad-hoc
consolidation batches while this service is active:

~~~bash
cd ~/spore-agentic-decompiler
bash enable_automatic_consolidation.sh --repo ~/spore-decomp --daily-usd 0.25
~~~

The installer prefers a persistent systemd user service, which resumes after
user login while WSL systemd is running. If systemd --user is unavailable,
it uses a background nohup process for the current WSL lifetime, which does
not survive a full WSL shutdown. Neither mode changes the SAD worker processes,
their OpenCode sessions, or the operational SQLite database.

Enabling this service authorizes ongoing paid DeepSeek requests. The default
estimated local cap is USD 0.25 per UTC day. It is not an enforced provider
credit ceiling. The existing OpenRouter key is used.

~~~bash
bash enable_automatic_consolidation.sh --repo ~/spore-decomp --status
bash enable_automatic_consolidation.sh --repo ~/spore-decomp --disable
~~~

Individual malformed model responses are recorded with their finding ID and
error reason, then retried with exponential backoff up to three failures.
Other findings proceed in the same batch. Network/configuration errors stop
the current batch to avoid repeated paid provider failures. The status command
also shows recent errors and their next eligible retry times. Charges are
settled from provider-reported usage even for rejected proposals.


Optional low-priority, continuous consolidation, started only explicitly:

~~~bash
.venv/bin/python work/scratch/spore_technique_consolidator.py \
  --repo . serve --daily-usd 0.25 --batch 3 --interval 180
~~~

This process uses nice +10 and an exclusive lock. It does not share active
OpenCode sessions, worker slots, or the worker budget ledger, although it does
share OpenRouter/provider rate limits with the fleet. It does not auto-start
on reboot. Ctrl+C stops it.

## Evidence and correctness

A linked MATCH proves the whole C++ function passed the independent
byte-exact checker. It does not prove that a particular model-extracted
technique caused the result. No number of matches and no model response can
automatically promote a technique to verified or reproduced. Techniques have
only hypothesis and observed classifications.

Each record describes a compiler problem, applicability conditions, attempted
intervention, observed effect and an exact documentary quote. Missing answers
must be described as not established, not invented. Links preserve the function
VA, result status and source-hash version. Invalid quotes, unknown technique
IDs and mismatched categories are rejected.

The model sees only bounded evidence and similar techniques, never the full
932-function corpus, entire conversation transcripts, or API credentials.

## Tests and limitations

~~~bash
cd ~/spore-agentic-decompiler
python3 -m unittest discover -s tests -p 'test_technique_consolidator.py' -v
~~~

GitHub Actions tests use mocked OpenRouter responses, no live paid calls.
They cover schema, quote validation, merges, provenance, budgets, model routing,
vector indexing, and Python 3.14 import compatibility. Live model output
quality and useful technique extraction rates still require a small paid
smoke test on your WSL machine.

References:

- https://openrouter.ai/deepseek/deepseek-v4.1-flash-1017
- https://openrouter.ai/docs/guides/features/structured-outputs
- https://openrouter.ai/blog/insights/model-routing/
- https://github.com/asg017/sqlite-vec
