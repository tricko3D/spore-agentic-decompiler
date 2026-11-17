# Spore Agentic Decompiler

AI-assisted, compiler-verified function matching tools for [NoahBPeterson/spore-decomp](https://github.com/NoahBPeterson/spore-decomp).

This repository currently contains the **SQLite parallel runner provider-recovery patch** for an existing `spore-decomp` checkout. It is not a standalone decompiler. It preserves historical attempts and verified matches while distinguishing upstream provider failures from actual C++ reconstruction attempts.

## What's included

- `spore_provider_fix/spore_recover_provider_attempts.py`: audits historical OpenCode logs and conservatively marks confirmed provider-only failures `VOID_PROVIDER`, without deleting their original attempt records.
- `spore_provider_fix/work/scratch/spore_sqlite_store.py`: SQLite WAL bookkeeping, attempt reviews, and concurrency slots.
- `spore_provider_fix/work/scratch/spore_first_pass_sqlite.py`: worker with provider-aware retries, distinct attempt logs, and per-function OpenCode session reuse.
- `spore_provider_fix/spore_parallel_sqlite.py`: configurable worker and inference concurrency.
- `spore_provider_fix/spore_matched_bytes.py`: retail-byte and matched-function progress reporting.
- `install_spore_provider_fix.py`: preview-first installer that backs up replaced scripts.

## Installation into an existing checkout

The original SQLite runner, its migration, and the compiler-check lock must already be installed in `~/spore-decomp`.

```bash
git clone https://github.com/tricko3D/spore-agentic-decompiler.git ~/spore-agentic-decompiler
cd ~/spore-decomp
cp ~/spore-agentic-decompiler/install_spore_provider_fix.py .
cp -a ~/spore-agentic-decompiler/spore_provider_fix .
.venv/bin/python install_spore_provider_fix.py
.venv/bin/python install_spore_provider_fix.py --apply
```

After all existing workers have stopped, preview the recovery before applying it:

```bash
.venv/bin/python spore_recover_provider_attempts.py --show 30
.venv/bin/python spore_recover_provider_attempts.py --apply
.venv/bin/python spore_parallel_sqlite.py status
.venv/bin/python spore_matched_bytes.py
```

A new 32-worker batch with at most eight simultaneous inference slots:

```bash
.venv/bin/python spore_parallel_sqlite.py run \
  --workers 32 --max-inflight 8 --passes 3 --reasoning high \
  --minutes 5 --quota-wait 1 --provider-retries 2 \
  --budget 20 --unsafe
```

## Safety and limitations

- Recovery creates a SQLite online backup before making changes. The original attempt records and logs are retained.
- Only passes with explicit `ttft_deadline_guard` retry evidence and no model or tool output are invalidated automatically. Missing or ambiguous logs stay unchanged.
- OpenCode **conversation** sessions are reused for the same function. The runner still creates a new OpenCode CLI process/private server for each pass, so it does not eliminate process startup overhead.
- The concurrency cap is not a guarantee against upstream throttling. API keys, databases, logs, generated sources, and machine-specific files should not be committed here.
- This patch has been locally syntax-checked and tested with a simulated SQLite recovery database. It has **not** been validated against every version of a live `spore-decomp` checkout.

See [README_PROVIDER_RECOVERY.txt](README_PROVIDER_RECOVERY.txt) for further details.
