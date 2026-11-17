# Spore Agentic Decompiler

A standalone **command-line controller** for AI-assisted, compiler-checked C++ reconstruction against [NoahBPeterson/spore-decomp](https://github.com/NoahBPeterson/spore-decomp). It does **not** contain or replace the game binary, retail fingerprints, or the upstream build system. It always works against a separate, existing `spore-decomp` checkout.

Designed for Linux / WSL2 with Python 3.10+, an installed [OpenCode](https://opencode.ai/) CLI, and a working upstream Spore compile/check environment (including the dependencies required by `tools/contrib/check.py`). No extra Python packages are required for this controller.

## Install

```bash
# From WSL or Linux
git clone https://github.com/tricko3D/spore-agentic-decompiler.git
cd spore-agentic-decompiler
python3 spore_agentic.py setup --repo ~/spore-decomp
```

`setup` prompts for an OpenRouter API key (hidden input) if one is not already provided via `OPENROUTER_API_KEY` or saved locally. It selects conservative defaults of **5 workers, 4 simultaneous inference slots, 3 real passes per function, and a $20 soft budget**. Override these with `--workers`, `--max-inflight`, `--passes`, `--minutes` and `--budget` during setup or at run time.

The API key, if entered interactively, is saved to `~/.config/spore-agentic-decompiler/openrouter.key` with permission `0600`. The repository checkout never receives the key. For environments with managed credentials, supply `OPENROUTER_API_KEY` instead. Configuration is in `~/.config/spore-agentic-decompiler/config.json`; `XDG_CONFIG_HOME` is supported.

`setup` detects an existing `work/scratch/codex-pass1/spore.sqlite3` database and **preserves it**. If there is no database but old `queue.csv`/`results.csv` files are present, it imports them without deleting them. For an unused upstream checkout with no prior queue, it builds a new SPGonzago-only queue from the upstream fingerprint index and module mapping, then migrates it to SQLite. It refuses an incomplete or ambiguous old queue rather than guessing at missing history.

The controller installs the bundled worker scripts and a narrow cross-process checker lock into the selected upstream checkout; replaced scripts and the original checker are backed up under `work/scratch/codex-pass1/sqlite-backups/`. **Stop all agents before setup/recovery.** An unexpected upstream checker layout makes setup fail closed rather than attempting a blind rewrite.

## Run and inspect

```bash
python3 spore_agentic.py doctor
python3 spore_agentic.py plan                    # no API calls
python3 spore_agentic.py recover                 # audit only, no changes
python3 spore_agentic.py recover --apply         # backup DB, annotate proven errors
python3 spore_agentic.py run --workers 5 --max-inflight 4 --budget 20
python3 spore_agentic.py status
python3 spore_agentic.py progress
```

`run` **incurs OpenRouter charges** and uses the existing upstream checkout to write real C++ and invoke the upstream compiler verifier. It currently supports the **SPGonzago** scope, using DeepSeek V4.1 Flash (`deepseek/deepseek-v4.1-flash-1017`) through OpenRouter, with the currently pinned Sail Research route. Model/provider selection, arbitrary modules, and fully persistent OpenCode *processes* are not yet configurable.

The worker preserves the OpenCode **conversation session** per function between passes. It starts a new CLI process for each pass, so this is *not* a persistent OpenCode server. Worker count and simultaneous API calls are separate controls. Provider `ttft_deadline_guard` failures are recorded as incidents and do **not** consume a legitimate decompilation pass; no-source retries can be reclaimed with `recover --apply` if their saved logs contain unequivocal evidence. The original attempt rows and logs remain available, with invalidated rows annotated as `VOID_PROVIDER`.

The **$20 budget is a local soft limit** using OpenRouter key-usage accounting, not a provider-enforced spending cap. For a hard cap, configure a limit on the key in OpenRouter itself.

## Existing-state diagnostic export

If you have an existing database, you can package it and the relevant local scripts for migration troubleshooting:

```bash
python3 spore_agentic.py diagnostics \
  --repo ~/spore-decomp \
  --output /mnt/c/Users/patrick/Downloads/spore-agentic-diagnostics.zip
```

This produces a consistent SQLite snapshot using SQLite's backup API, plus the existing queue/results and runner scripts. **It does not include the API key, source trees, or raw OpenCode JSONL logs.** The archive may still contain private attempt history and machine paths, so inspect it before sharing.

## Safety and known limits

This is a working controller around the existing targeted Spore matching workflow, **not a universal binary decompiler**. It needs the retail reference data and a functional compiler-check setup provided by the upstream project. Agents execute commands and modify C++ inside the selected checkout, so keep it under version control and review changes before merging. Functions assigned to different workers may still share dependencies, and the checker lock serializes compilation rather than fully isolating each agent. Only explicit successful byte matches should be considered verified.

The recovery classifier is deliberately conservative: missing logs and logs containing any actual agent output are not automatically voided. The standalone setup, migration, and recovery paths have offline tests; live OpenRouter/model performance is environment-dependent and has not been certified against every upstream commit. No game assets, binaries, or secrets are included in this repository.
