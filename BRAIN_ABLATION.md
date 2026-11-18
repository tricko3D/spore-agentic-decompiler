## Recovery: prepared trial contains historical attempts (WAL copy defect)

Before the trial runner starts, the cloned source SQLite DB can have a WAL containing the deleted historical attempts. Older trial-copy code used shutil.copy2() on the seed's .sqlite3 file but ignored its -wal file. This copied old pages, resurrected the archived attempts, and stopped with "Old target attempts leaked into trial." The **same bug could also omit knowledge redaction**, so these are protocol-integrity failures rather than decompilation failures.

Trial cloning now uses SQLite's online backup API, copying committed logical state of both the operational and knowledge databases, including changes present only in WAL. Regression tests demonstrate the old file copy resurrecting history and the fixed backup preserving deletion and knowledge masking.

If the user hit this before vector-service startup on the first trial, the unstarted clone directory remains on disk with its frozen source, preflight configuration, and untouched raw seed SQLite base file. Running the SAME suite command again automatically audits that precise state, archives the old trial ID in invalid_history, and gives it a fresh trial sandbox before starting inference. It never overwrites the failed clone. The original recovery guard compared raw SQLite SHA256 hashes with the seed. That was too strict: SQLite can rewrite its header or checkpoint pages during a simple read, and the old trial may contain historical rows that were only removed in the seed's WAL. The recovery guard now compares the trial's **logical operational records** to the frozen pre-experiment historical archive, allowing only identical old rows or subsets of them. It explicitly requires old target attempts to be present. It **refuses** any unarchived attempt, session, spend record or other activity row, any agent log, Brain query log, modified C++ source or mismatched randomized configuration. It keeps the retired directory and an invalid-history record, never labels it a measurement. A genuinely started experiment must be manually investigated and cannot be silently replayed. All already frozen suite source/knowledge snapshots remain unchanged.

## One-command known-solvable suite

**Main experiment, separate from the unsolved-function pilot.** The study uses three previously matched functions: 0x00c45760, 0x00c2f1f0, and 0x00b8d190. Each had four failures before an independently accepted MATCH in the locally reported history. The suite verifies this chronology against the source checkout's SQLite history. It creates four conditions (baseline, raw findings, consolidated findings/techniques, unrelated placebo evidence), three repetitions per function, and 36 total trials.

To execute the entire suite locally with one command, including preparation, execution and final comparison:

~~~bash
cd ~/spore-agentic-decompiler
git pull --ff-only
python3 brain_ablation_suite.py run --repo ~/spore-decomp \
  --out ~/sad-known-solved-suite --repeats 3 --execute --budget 0.50
~~~

The suite deliberately does **not** run paid agents without --execute. Without that flag it prepares all three heldout sources, randomized arms, frozen knowledge and a unified report, and then exits after a dry run. The per-trial soft guard is USD 0.50, so the sum of 36 per-trial budgets is USD 18. This is not a guaranteed total billing ceiling. The execution happens in the invoking local terminal, it is not an asynchronous service.

To regenerate the comparison from the recorded trials, offline and without further model use:

~~~bash
python3 brain_ablation_suite.py compare --out ~/sad-known-solved-suite
~~~

Outputs: suite-results.csv (all trials, evidence and metrics), suite-report.json (full data), and one sub-benchmark in functions/<address>/ per known-solvable function. Each sub-benchmark has frozen source history and trial logs. The global schedule deterministically interleaves the three targets while preserving random arm order within each target. On a blocked, invalid, or interrupted trial, the command stops rather than silently replacing the observation. Re-running the same command skips completed trials, and requires an audit before advancing past an invalid attempt.

**Primary endpoint:** independently verified byte-perfect MATCH rate by function and arm. Checker calls, tokens, and spending are secondary; fewer tokens for a NO MATCH are not counted as evidence of better decompilation. This is a small pilot, not a statistically powered experiment.

**Holdout safeguards:** Preparation checks for an accepted MATCH after at least three failures, an isolated source file, and an existing MATCH manifest entry for every target. All three original solved C++ bodies are blanked from every experimental seed, and their prior manifest entries are removed **in the copies only** so the runner will investigate rather than skip. All prior attempts and sessions are cleared in every experimental SQLite copy. The cloned task.py is patched to suppress prior notes and closed pull request summaries that could reveal solutions. One WAL-consistent knowledge snapshot is frozen once for the entire suite, its checksum verified before resuming, and the same snapshot is used for all three functions. The cloned knowledge DB is pruned of these targets' own findings, linked compiler techniques, full-text entries and vector embeddings. Unrelated knowledge remains; live source and archived originals remain unchanged.

Because OpenCode's agent tools can access the broader filesystem and public networks, this is not a fully adversarially isolated, blinded experimental environment. The controlled prompt prohibits looking up public historical solutions and the ordinary local leakage paths are removed. Unexpected easy MATCHes should still trigger a review of agent tool logs.


# SAD Brain ablation benchmark

## Research question

Does agent access to evidence-backed consolidated MSVC techniques improve independently checked Spore byte-match results compared with no knowledge, raw investigation notes, or unrelated evidence of comparable length?

The agent makes its own technical retrieval queries during the run. No answer is preselected or pasted into its initial prompt. Source code and outcome history start from the same blank target in every arm. Retrieval source is the independent Collective Memory, not the operational decompilation database.

## Four treatment arms

| Arm | Available through the benchmark Brain query tool |
| --- | --- |
| baseline | No knowledge, tool returns an unavailable notice |
| raw | Semantic retrieval of original findings only; technique tables are cleared in the trial copy |
| consolidated | Semantic retrieval of original findings plus the consolidated compiler techniques and evidence links |
| placebo | Unrelated, real findings and techniques with a response length calibrated to real retrieval for that query |

The vector database and its precomputed embeddings remain in the raw, consolidated and placebo copies. The underlying index is *frozen* for the pilot, pending embedding jobs are cleared only in the trial copies. Each non-baseline arm starts a separate local embedding server and **aborts before a paid agent call** if it cannot start, rather than silently downgrading to lexical-only retrieval.

The target's own historical findings remain semantically discoverable in raw/consolidated memory, but its exact address is replaced with a stable pseudonymous address in cloned memory tables, FTS records and evidence labels. Direct target-address queries are rejected by the benchmark query tool. This is intentional: the experiment tests whether agents can reason with earlier failures when they discover those failures through technical relevance, not by directly loading the previous target history.

## Protocol v2: runner-owned claims are not evidence

The production prompt contains the instruction to return `SKIP: claimed` whenever `task.py` reports a claimed function. However, this runner must claim its assigned function before launching the agent. In the original scientific pilot, the raw-findings agent replied `SKIP: claimed`, made no Brain calls, and did no compilation. Its automatic post-run checker then reported no compiled function. This is a **protocol defect**, not a decompilation failure or evidence for/against consolidation.

Protocol v2 patches the **copied experiment prompt only** so the runner's own claim is explicitly expected. It retains the claim lock for correct concurrency and forbids modifying or releasing it. The agent must investigate the exact assigned target and is instructed to query the Brain before its first compiler hypothesis, even in the baseline where no knowledge will be supplied.

Protocol v2 installs a **canonical benchmark-only prompt** from `brain_ablation_prompt.txt`, independent of the text in the user's local `spore_first_pass_prompt.txt`. Earlier code searched for an exact `if claimed, reply SKIP` sentence and could fail when that local prompt differed. This canonical prompt explicitly treats the runner's own claim as expected, requires the Brain query before the first compiler hypothesis, and restricts edits to the selected target. It replaces only the copied experiment prompt, never the installed live Spore prompt. Its SHA-256 is identical for every treatment arm. The old protocol v1 experiment and its terminal SKIP remain archived; its result should be reported in a **protocol-validation log**, not interpreted as evidence of a measured difference between knowledge conditions.

To run a new and internally consistent study, use a **new** experiment output directory instead of changing only the remaining v1 trials:

```bash
cd ~/spore-agentic-decompiler
git pull --ff-only
python3 brain_ablation.py prepare --repo ~/spore-decomp \
  --out ~/sad-brain-benchmark-v2 --repeats 3 --va 0x00b848f0
python3 brain_ablation.py run-next --out ~/sad-brain-benchmark-v2
```

If preparation already failed at `_patch_prompt` after cloning, the experiment may be recovered without deleting or recopying the repository. This is a different partial stage from the earlier symlink failure: the SQLite backups have already been installed, previous attempts cleared, and knowledge masked. The guarded resume validates those facts, keeps the frozen SQLite bytes untouched, and completes prompt installation and plan creation:

```bash
cd ~/spore-agentic-decompiler
git pull --ff-only
python3 brain_ablation.py prepare --repo ~/spore-decomp \
  --out ~/sad-brain-benchmark-v2 --repeats 3 --resume-partial
python3 brain_ablation.py run-next --out ~/sad-brain-benchmark-v2
```

The recoverable directory must have `archive/` and `seed/` but no `plan.json` or `trials/`. A modified target source, an existing trial, old sessions or unexpected database state blocks recovery rather than mixing evidence or overwriting results.

The above is preparation and a dry run only. If the target's 7-failure eligibility changes, `candidates` shows other targets. Protocol v2 refuses to execute old v1 plans, even if their remaining arms are still prepared, to prevent contamination by mixed prompts. Re-running the original v1 trial selectively as if it were a missing baseline would compromise randomization.

A valid *test of whether the knowledge was used* still requires successful Brain tool invocation and retrieved evidence. A mere assignment to the raw or consolidated arm is not proof of exposure. Protocol v2 prompts the agent consistently across arms but does not force its tool calls; the report tracks this distinction. An additional protocol-validity gate or controlled query mechanism would be needed to guarantee equal knowledge delivery without relying on voluntary agent behavior.

## Preparation, free and non-destructive

Prerequisites: SAD installed inside the Spore checkout, the independent Collective Memory database initialized with consolidated techniques, a working local embedding service environment, and a target with at least seven genuine failed/deferred attempts whose implementation lives in its own dedicated `match/slices/sXXXXXXXX/sXXXXXXXX.cpp` file.

From the SAD repository:

```bash
cd ~/spore-agentic-decompiler
python3 brain_ablation.py candidates --repo ~/spore-decomp
python3 brain_ablation.py prepare --repo ~/spore-decomp \
  --out ~/sad-brain-benchmark --repeats 3
python3 brain_ablation.py run-next --out ~/sad-brain-benchmark
```

Alternatively specify `--va 0xXXXXXXXX` in the prepare command after identifying an eligible standalone slice. The target selection chooses the highest failed-attempt count among eligible dedicated slices, not an arbitrary function or one already marked as a MATCH.

Prepare uses SQLite's online backup API to archive the WAL-consistent original `spore.sqlite3` and `knowledge.sqlite3`. It also archives the original C++ slice, its SHA-256 hash and target history. It copies the upstream checkout to a seed under the experiment directory, removes old operational attempts/sessions **only in that copy**, blanks the dedicated target source **only in that copy**, masks the target address in the copied knowledge DB and installs a benchmark-specific Brain query tool. Other stored technical evidence and vectors are kept intact. It never writes to the live Spore checkout and never calls an LLM.

Internal symlinks, including absolute links from `work/match` back to files in the source checkout, are retargeted to equivalent paths inside the isolated seed. The same relocation is checked again for every trial clone. If a symlink points to a source file that was excluded from the copy, preparation stops rather than leaving a write-through path into the live checkout.

**Recovering an interrupted preparation:** If preparation previously failed at the symlink safety check after copying the checkout, you can reuse its existing WAL-consistent backups and full seed copy without running another expensive copy:

```bash
cd ~/spore-agentic-decompiler
git pull --ff-only
python3 brain_ablation.py prepare --repo ~/spore-decomp \
  --out ~/sad-brain-benchmark --repeats 3 --resume-partial
```

This only resumes a specifically validated pre-database-install state with the original target source, original runner and archives unchanged. It refuses to modify completed experiments, partially executed trials or a changed source checkout. It does not launch any paid runs.

A full checkout is copied (excluding ephemeral logs, runtime files, prior investigation snapshots, caches, VCS metadata and live SQLite/WAL files). The installed decompiler Python virtualenv and the separate FastEmbed/sqlite-vec virtualenv under memory/.venv are shared dependency symlinks, not experiment source trees. The model weights and vector database are copied independently. Each trial receives its **own independent copy** of the seed and its own databases, agent sessions, source and logs. No Git worktrees or hardlinks point agent edits into live source. Expect substantial disk usage and copy time, especially for many trials.

The plan randomizes the four arm orders per repeat using a recorded seed, and assigns opaque trial directory names to avoid advertising the treatment in the agent's working directory or process environment. It defaults to three repeats (12 independent trials). All experiment output is in `~/sad-brain-benchmark`, never the source repository.

## Paid execution, explicitly opt-in

For each trial:

```bash
python3 brain_ablation.py run-next --out ~/sad-brain-benchmark --execute --budget 0.50
python3 brain_ablation.py report --out ~/sad-brain-benchmark
```

Only `run-next --execute` launches an OpenRouter decompilation agent. The default uses the existing DeepSeek V4.1 Flash 1017 / Sail Research runner, high reasoning effort, one fresh pass per independent trial, the same isolated checker, the same tools and a $0.50 per-run **soft** budget. The stored private SAD OpenRouter key (permission 0600) is used when the key is not exported. No key is saved in any experiment artefact.

The existing OpenRouter key's provider-side spending limit remains the actual hard backstop. The per-run guard can include other spending on the same key, so **pause the consolidator and other paid workers during the benchmark** if you want reliable cost comparisons. Similarly avoid changing shared toolchain versions between trials. Do not delete the source or operational SQLite database in the live Spore checkout.

The command runs exactly one randomized treatment each invocation. Before any paid request in a knowledge arm, a copied offline embedding service must reach ready status. It also refuses to resume an existing sandbox silently. There is no auto-purchasing from prepare, candidates, report, or the dry-run version of run-next.

### Long Unix socket path in isolated benchmark copies

The original Collective Memory daemon binds `work/scratch/codex-pass1/memory/memory.sock`. In a deep cloned trial directory the absolute socket pathname can exceed Linux's ~107-byte `AF_UNIX` filesystem socket limit. This triggers `OSError: AF_UNIX path too long` **before the paid model runs**.

For checkouts where that socket path would be too long, the Brain now uses a deterministic, per-checkout socket such as `/tmp/sad-memory-1000/<20-hex-digit-hash>.sock`. The prefix directory must be owned by the current Unix user with mode `0700`. The socket is mode `0600`. It is derived from the canonical trial path so concurrent and sequential clones never connect to each other's socket. The original SQLite database path, model cache, embedding vectors and evidence are **unchanged**. Normal-length production checkout paths keep the existing socket location.

The benchmark no longer assumes `memory.sock` is inside the clone when checking service readiness. It queries the memory service's `status` using the same patched path resolution as the agent. Short-path changes are applied to the **isolated memory code only**, leaving the installed live Spore checkout unchanged.

Old benchmarks prepared before this fix can be repaired safely, without recopying the source or reinitializing SQLite:

```bash
cd ~/spore-agentic-decompiler
git pull --ff-only
python3 brain_ablation.py repair-seed --out ~/sad-brain-benchmark
python3 brain_ablation.py retry-invalid --out ~/sad-brain-benchmark
python3 brain_ablation.py run-next --out ~/sad-brain-benchmark
```

The last command is a dry run. The previous aborted attempts, including `trial-retry-b0f9ffcaa2c74a80`, remain archived and do not count against the experiment. `repair-seed` refuses to modify shared tooling after any real agent activity.

### Repairing the original seed after a launcher upgrade

If a trial reports `can't open file .../brain_trial_entry.py`, its seed was prepared before the safe Python 3.14 launcher was introduced. Updating the SAD checkout does **not** retroactively update `~/sad-brain-benchmark/seed`. This makes repeated retries fail in exactly the same way.

The offline, non-destructive repair verifies that every current and retired trial lacks agent output, Brain queries, recorded attempts, or provider spending before changing **only** three benchmark tools in the seed: `brain_trial_entry.py`, `brain_trial_query.py`, and the benchmark-specific query command in `spore_first_pass_sqlite.py`. It records SHA-256 checksums of these tools in `plan.json`. All frozen operational/knowledge SQLite files, original source, treatment assignments and failed trial logs remain intact.

```bash
cd ~/spore-agentic-decompiler
git pull --ff-only
python3 brain_ablation.py repair-seed --out ~/sad-brain-benchmark
python3 brain_ablation.py retry-invalid --out ~/sad-brain-benchmark
python3 brain_ablation.py run-next --out ~/sad-brain-benchmark
```

The final command is a dry run and performs no paid inference. After the user deliberately adds `--execute --budget 0.50`, it creates a new isolated sandbox from the updated seed. `repair-seed` refuses to operate if a trial has genuinely started, since mixing different tooling versions would invalidate comparisons.

### Python 3.14 `dis.py` collision

The Spore checkout contains `work/scratch/dis.py`, which is a project helper and **not** Python's standard-library `dis`. Running `python work/scratch/spore_first_pass_sqlite.py run` directly puts that helper's directory at the front of `sys.path`. Under Python 3.14, `argparse` imports `inspect` and `dis` during CLI setup, so this can exit before the agent is started with `ValueError: invalid literal for int() with base 16: 'run'`.

The benchmark now launches the runner, Brain queries and memory service through the small `brain_trial_entry.py` script at the root of **each isolated trial**. It appends `work/scratch` after stdlib and site-packages, and executes the actual original script via `runpy.run_path`. This preserves original runner arguments and working directory while keeping Python's own `dis` ahead of the Spore helper. The production checkout and `work/scratch/dis.py` remain unchanged.

### Infrastructure failures and retry

The Brain service is a strict, offline dependency for raw, consolidated and placebo arms. If its subprocess exits before socket readiness, the harness does not invoke any paid agent and now prints a bounded, credential-redacted tail of `work/scratch/codex-pass1/benchmark-memory-service.log` directly in the terminal. The full log remains in that trial's directory, and the startup traceback is required to identify the actual failure, whether model-cache loading, sqlite-vec, import errors, or another issue.

A nonzero runner exit or a zero exit without a recorded attempt is an **invalid trial**, not a FAIL or a zero MATCH in the experimental denominator. The runner prints a bounded diagnostic tail. The complete `benchmark-run.log` is retained in the original sandbox:

```bash
tail -n 80 ~/sad-brain-benchmark/trials/trial-00-b40b0394/benchmark-run.log
```

Once the underlying infrastructure problem is corrected, requeue the **same arm and repeat** into a fresh independent copy of the original seed:

```bash
python3 brain_ablation.py retry-invalid --out ~/sad-brain-benchmark
python3 brain_ablation.py run-next --out ~/sad-brain-benchmark
```

This prepares, but does not execute, the replacement. The old invalid sandbox and log stay intact and its name is kept in `plan.json` under `invalid_history`. Retry refuses any attempt containing a recorded result, an agent JSONL trace, Brain queries or measured provider spending. If there was meaningful agent activity, investigate the attempt manually rather than silently rerunning it.

The report uses `match: null` for `NO_RESULT` trials and excludes these from match-rate denominators. A `0/1 MATCH` result must represent a genuinely recorded nonmatching attempt, not an infrastructure crash.


## Evaluation
## Evaluating SKIP and treatment exposure

An agent may legitimately report `SKIP` (e.g. shared-header changes forbidden by the runner) before attempting compiler experiments. The normal Spore checker still runs after the agent terminates, but a `SKIP` is an **agent outcome**, not proof of a byte mismatch. The previous `CHECKS` counter only counted `check.py` commands in the **agent JSONL**, not the runner's post-agent verifier. The report now labels this `AGENT CHECKS`, prints the recorded SQLite outcome explanation and retains the unabridged note in `report.json`.

A trial with 0 Brain queries in a knowledge treatment never exercised retrieval. It remains in the **intention-to-treat** operational match-rate denominator as an unmatched run, but is tagged `treatment_adherent: false` and `exposure: not_queried`. Real raw treatment exposure requires at least one original finding returned; consolidated treatment exposure requires at least one consolidated technique returned. Placebo exposure requires a nonempty placebo response. The baseline deliberately lacks knowledge and is not subject to that treatment-use requirement.

Do not selectively drop or rerun `SKIP` outcomes to improve an arm's observed match rate. If the agent's stated SKIP reason reflects a common early blocker, investigate it before making a new, consistently applied experimental protocol. Existing completed results and their original prompts must remain archived. A stronger study can analyze both intention-to-treat (assignment) and treatment-used cohorts, reporting the latter as observational rather than a randomized causal estimate.

For an example pilot with `raw, repeat 3, SKIP, 0 agent checker calls, 0 Brain queries`, the valid conclusion is only that the runner delivered a terminal SKIP and did not expose raw evidence to the agent. It cannot establish that retrieving or consolidating knowledge was ineffective.


```bash
python3 brain_ablation.py report --out ~/sad-brain-benchmark
```

Results include verified MATCH vs FAIL, checker calls observed in JSONL tool events, agent-reported input/output tokens when available, actual Brain queries and provider key-usage-based soft cost. Original runner notes retain independent checker size/difference details. An additional JSON report is written to `report.json`.

The first-pass agent's subjective narrative is not considered a match: the runner calls the independent retail checker and records a real MATCH only after verifying it. Empty or provider-failed trials remain distinguishable from genuine failures. Trials with no Brain queries do not demonstrate access to knowledge, even if classified into a knowledge arm.

Compare consolidated against raw for added value from abstraction, consolidated against baseline for usefulness, and consolidated against placebo to detect context-bloat effects. For a convincing conclusion, expand beyond one function and replicate across distinct failure modes. The first 12-trial run is a **pilot**, not statistically significant proof of broad improvement.

## Validity and known limitations

* OpenCode executes shell commands without an OS sandbox. The benchmark prompt and cloned databases enforce the intended *interface*, but cannot cryptographically prevent an agent deliberately reading unrelated files or bypassing the Brain query tool. For publication-grade leakage prevention, run each copy inside a filesystem-isolated container or VM with access controls, use separate providers/keys and audit tool calls.
* Exact output token equality across placebo and consolidated search calls is not guaranteed. The placebo uses real unrelated evidence, samples deterministically for each agent-written query and matches output character length approximately. Token-level equalization can be added if trials indicate context length is a large confound.
* No model seed control is exposed by the existing OpenCode runner, so repetitions and randomized treatment order are essential.
* Source snapshots from a running upstream checkout can contain independently changing non-SQLite files. It is safest to prepare when other decompiler agents are stopped; consistent SQLite snapshotting alone does not freeze source files.
* Pseudonymizing the address masks direct target lookups in the copied memory, but does not remove target-related semantic embedding signals, by design. Existing raw evidence can still reveal the target's historical failure mechanisms.
* Token counts depend on the OpenCode event schema. The JSON report treats missing usage as unverified rather than estimating purchased tokens. A shared provider key can distort costs through unrelated requests.

No results are claimed until the paid experiments have actually run.
