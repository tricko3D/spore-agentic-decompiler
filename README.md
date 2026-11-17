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

The API key, if entered interactively, is saved to `~/.config/spore-agentic-decompiler/openrouter.key` with permission `0600`. The repository checkout never receives the key. For environments with managed credentials, supply `OPENROUTER_API_KEY` instead. After rotating a key, `python3 spore_agentic.py rotate-key` also updates an existing saved key file, so a running dashboard can use the replacement at its next Launch. For environment-only keys, the dashboard must be restarted to inherit the new environment; alternatively use `rotate-key --save-key` to explicitly save the replacement in the private key file and avoid restarting. The dashboard never resets spending or attempts when resolving a key. Configuration is in `~/.config/spore-agentic-decompiler/config.json`; `XDG_CONFIG_HOME` is supported.

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

`run` **incurs OpenRouter charges** and uses the existing upstream checkout to write real C++ and invoke the upstream compiler verifier. It defaults to **SPGonzago** but can select other confidently classified modules via `scope --module NAME --apply` or the dashboard control panel. The model remains DeepSeek V4.1 Flash (`deepseek/deepseek-v4.1-flash-1017`) through the pinned Sail Research route. Fully persistent OpenCode *processes* are not implemented.

## Sail Research adaptive throughput (default)

The **worker count** is the available local pool. `--max-inflight` is now an **upper ceiling**, not the starting concurrency. A new paid run starts with **one** simultaneous OpenRouter agent pass and increases the shared SQLite cap by one after repeated clean agent passes, with at least 90 seconds between probes. Upstream HTTP 429 events (including `ttft_deadline_guard` embedded in an HTTP 200 stream) trigger a **multiplicative decrease** (halve the limit), followed by a shared 60 to 900 second exponential cooldown. Workers cannot start a new OpenRouter pass during cooldown, even if a slot looks free.

```bash
python3 spore_agentic.py run --workers 16 --max-inflight 16 --passes 3 --budget 20
python3 spore_agentic.py status
```

Here 16 is an upper bound and the controller may settle at 1, 3 or any other sustainable number. The actual usable cap varies by Sail availability. It measures **whole OpenCode pass outcomes**, not individual internal OpenRouter requests, so adjustments are deliberately conservative and may lag sudden changes in provider capacity. It does not guarantee zero TTFT errors, nor is it a guarantee of optimal dollars per match. Fixed-cap behavior is still available via `--fixed-inflight`. Each new run begins a fresh capacity search; the historical attempts, provider-error incidents, and expense tracking persist separately.

Confirmed provider-only failures never consume a matching pass. In adaptive mode workers pause and retry those passes after the shared cooldown instead of permanently exiting after a fixed number of 429s. Temporary failures of the OpenRouter `/key` spending guard also pause requests until the budget can be validated again; authentication errors and insufficient credits still stop the worker. The OpenRouter API key limit remains the true hard spending cap, while `--budget` is a local soft limit. Long unattended runs require the host computer and WSL environment to remain awake.

The worker preserves the OpenCode **conversation session** per function between passes. It starts a new CLI process for each pass, so this is *not* a persistent OpenCode server. Worker count and simultaneous API calls are separate controls. Provider `ttft_deadline_guard` failures are recorded as incidents and do **not** consume a legitimate decompilation pass; no-source retries can be reclaimed with `recover --apply` if their saved logs contain unequivocal evidence. The original attempt rows and logs remain available, with invalidated rows annotated as `VOID_PROVIDER`.

The **$20 budget is a local soft limit** using OpenRouter key-usage accounting, not a provider-enforced spending cap. For a hard cap, configure a limit on the key in OpenRouter itself.

## Mission Control dashboard

The local dashboard has a **read-only monitoring mode** and an explicitly enabled **run-control mode**:

```bash
python3 spore_agentic.py dashboard                   # read-only, safe alongside an active run
python3 spore_agentic.py dashboard --control         # launch / stop / choose module
```

Open `http://127.0.0.1:8765` in the browser. Both modes show a live grid of the latest run's workers, with a compact preview of each agent's recent OpenCode events. Select an agent to view a longer live stream and its worker status. Inactive worker cards show **OFFLINE** in red; the card also shows its last known function and size where available. The grid works with a terminal-launched run as well as runs started in the browser. Raw tool commands and full tool output are **not** served. Agent text is filtered for obvious key-like strings, but may still contain checkout-specific details, so do not expose the server to a network.

The results explorer lets you filter all queued functions by their latest valid **MATCH / FAIL / DEFER / SKIP / PENDING** status, view the `+1` through `+5` failure-effort distribution, see function and outcome **byte totals**, and inspect pass history (including `VOID_PROVIDER` annotations) and recorded technical failure reasons. For failures, the byte-agreement column displays values like **58/59 bytes** only when a saved checker comparison provides matching binary sizes and an explicit differing-byte count. Historical agent `check.py` tool output may be used where the independent verifier headline lacks those numbers, and is labeled as potentially predating the final candidate. Size mismatches are marked as such, never presented as matching bytes. New worker versions preserve concise independent verifier size/diff evidence and longer failure explanations. The browser does not run the compiler to synthesize missing diffs. The cost cards group each function's *captured OpenCode step costs* by its latest outcome, including previous attempts on that function, and show coverage. Some OpenCode versions omit the final `step_finish`, so these are **lower-bound estimates** rather than precisely attributed OpenRouter charges. The separate OpenRouter API-key usage total remains authoritative for cumulative cost.

When `--control` is provided, the dashboard can select a module, add its classified functions to the existing SQLite queue through the usual safe `scope --apply` operation, launch an adaptive paid batch, or gracefully stop the active coordinator. Start/stop actions require a random session token and exact localhost origin and host checks; the server listens only on `127.0.0.1`. It never accepts arbitrary shell commands or user-supplied process IDs. Controls remain disabled by default; shutdown leaves recorded results intact. Closing the dashboard does **not** stop an already launched decompilation run.

The dashboard is built entirely with the Python standard library and polls the WAL database without holding a write lock. Live streams are read from bounded tails of existing JSONL logs.

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
