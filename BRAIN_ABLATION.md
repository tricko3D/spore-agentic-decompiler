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

A full checkout is copied (excluding ephemeral logs, runtime files, prior investigation snapshots, caches, VCS metadata and live SQLite/WAL files). The installed Python virtualenv is a shared dependency symlink, not an experiment source tree. Each trial receives its **own independent copy** of the seed and its own databases, agent sessions, source and logs. No Git worktrees or hardlinks point agent edits into live source. Expect substantial disk usage and copy time, especially for many trials.

The plan randomizes the four arm orders per repeat using a recorded seed. It defaults to three repeats (12 independent trials). All experiment output is in `~/sad-brain-benchmark`, never the source repository.

## Paid execution, explicitly opt-in

For each trial:

```bash
python3 brain_ablation.py run-next --out ~/sad-brain-benchmark --execute --budget 0.50
python3 brain_ablation.py report --out ~/sad-brain-benchmark
```

Only `run-next --execute` launches an OpenRouter decompilation agent. The default uses the existing DeepSeek V4.1 Flash 1017 / Sail Research runner, high reasoning effort, one fresh pass per independent trial, the same isolated checker, the same tools and a $0.50 per-run **soft** budget. The stored private SAD OpenRouter key (permission 0600) is used when the key is not exported. No key is saved in any experiment artefact.

The existing OpenRouter key's provider-side spending limit remains the actual hard backstop. The per-run guard can include other spending on the same key, so **pause the consolidator and other paid workers during the benchmark** if you want reliable cost comparisons. Similarly avoid changing shared toolchain versions between trials. Do not delete the source or operational SQLite database in the live Spore checkout.

The command runs exactly one randomized treatment each invocation. Before any paid request in a knowledge arm, a copied offline embedding service must reach ready status. It also refuses to resume an existing sandbox silently. There is no auto-purchasing from prepare, candidates, report, or the dry-run version of run-next.

## Evaluation

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
