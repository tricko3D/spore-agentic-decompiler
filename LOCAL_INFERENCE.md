# Local inference with Inkling 2 Mini

SAD's optional `--provider local` uses an OpenAI-compatible server directly through
OpenCode. It does **not** send requests through OpenRouter or DeepInfra, check
their paid balances, or reserve a USD inference budget. Existing paid backends
remain unchanged.

## Measure throughput first, then run at the selected concurrency

On the `dev-local-inkling` branch, first stop the previous SAD process and
the prior llama.cpp server. Then:

```bash
cd ~/spore-agentic-decompiler
git pull --ff-only origin dev-local-inkling
python3 scripts/benchmark_local_parallel.py
```

The benchmark tests 1,2,3,4,6,8,12,16,24,32 true parallel llama.cpp slots (as memory permits), using
a **32K context per slot**, identical prompt workload at each setting, and
two measured rounds after an unscored warmup. It compares aggregate generated
tokens per wall-clock second, per-request generation rate, latency and RAM/VRAM
headroom. Startup and model-load times do not affect scores. Configurations
that fail to start, time out or leave insufficient memory are rejected.
If two successive configurations fail to fit, the benchmark stops increasing
parallelism. It also tests 4/6/8/10/12/16 CPU threads and 10/12/14 MoE CPU expert
layers at the best concurrency.

The winner is saved to `~/inkling-runtime/sad-parallel-profile.json`.

### Throughput in the existing SAD dashboard

The local model is started manually, then the existing dashboard is launched
with its normal controls:

```bash
cd ~/spore-agentic-decompiler
python3 spore_agentic.py dashboard --repo ~/spore-decomp --control
```

Select **Local** and click **Launch**. Each Agent card displays the
most recent output tokens/sec measured from that worker's OpenCode step log.
The aggregate is the sum of recent agent measurements. Until a step finishes,
the rate is unavailable (a dash), rather than a fabricated zero or the rate
of an unrelated llama.cpp slot. A model slot is not a worker, so the separate
slot grid has been removed. These completed-step rates include any time
spent using tools, so they are not identical to raw GPU decoding speed.

Run the 24-hour experiment from the measured profile:

```bash
python3 scripts/run_local_high_effort.py --workers auto --max-tools 30 --hours 24
```

The launcher starts the exact measured configuration and uses its **actual
per-slot** context, not total context. It records per-run MATCH and EQUIV
separately. You may still use `--workers 4` or another explicit number
to bypass the profile, but that is not benchmark-selected.

This launcher uses the existing llama.cpp server when it is already running;
otherwise it uses the locally installed Unsloth binary and cached Q4_K_M
GGUF with the **measured 31 tok/s MoE expert offload strategy**. It attempts
128K context, then 64K / 32K / 16K if the runtime cannot allocate it. Model ID,
actual context and output capacity are discovered automatically, with no
manual environment exports. The optimized 12GB-GPU settings use full core
offload, CPU experts on physical layers 0-5 and 18-23, quantized Q4 KV cache,
flash attention, a reduced microbatch, and Unsloth's recommended sampling.

The launcher installs/updates SAD's backed-up engine, checks local connectivity
and starts the measured number of concurrent agents, high-effort local reasoning,
12 compiler-check cycles as a soft prompt budget, a strict 30 model tool calls
per function, and a 60-minute **inactivity** watchdog. It runs the full queued
scope, with a common 24-hour deadline.
No paid provider calls are made. Other worker options are available through
`python3 scripts/run_local_high_effort.py --help`.

If the server is already running at 16K, the launcher will reuse its actual
16K limit rather than silently pretending it is 128K. Stop the old server to
let the launcher start the optimized 128K configuration.

## Start an inference server

**Important, model-format compatibility:** The root GGUF files from
`unsloth/Inkling-2-Mini-GGUF` use Unsloth's physical-layer layout.
Current upstream llama.cpp can fail to load these files with
`check_tensor_dims: tensor 'blk.24.attn_q.weight' not found`.
Unsloth's looped-model build supports this format. Do not redownload
the ~16.6 GB Q4_K_M model, and do not simply rebuild upstream llama.cpp.

For your WSL2 RTX 4070 Ti with CUDA 12.8, install Unsloth's verified
CUDA 12 portable prebuilt into a **separate** directory:

```bash
mkdir -p ~/inkling-runtime && cd ~/inkling-runtime
curl -fL --retry 3 -o llama.tar.gz \
  https://github.com/unslothai/llama.cpp/releases/download/b12588-mix-c0c3de8/app-b12588-mix-c0c3de8-linux-x64-cuda12-portable.tar.gz
mkdir -p llama.cpp
tar -xzf llama.tar.gz -C llama.cpp
./llama.cpp/llama-server --version
```

Reuse the already downloaded GGUF from the Hugging Face cache (the
exact path may differ between machines):

```bash
MODEL="$(find "$HOME/.cache/huggingface/hub/models--unsloth--Inkling-2-Mini-GGUF/snapshots" \
  -name 'Inkling-2-Mini-UD-Q4_K_M.gguf' -print -quit)"
test -n "$MODEL" && test -s "$MODEL" || { echo 'Model not found'; exit 1; }

./llama.cpp/llama-server \
  --model "$MODEL" \
  --host 127.0.0.1 --port 8080 \
  --jinja --ctx-size 16384 --n-gpu-layers 8 --parallel 1 \
  --temp 1.0 --top-p 1.0 --top-k 0 --min-p 0.0
```

This uses CPU RAM for layers not fitting in 12 GB VRAM. The optional
vision projector and DSpark drafter are omitted for the first text-only,
single-worker diagnostic. Increase GPU offload only after checking RAM,
VRAM, KV cache, and actual throughput. For two server slots, make sure
each has sufficient context and memory, for example with 32768 total
context and `--parallel 2` after the single-slot test succeeds.

Alternatively, use the `Mainline_Support` GGUF variant and the
`ggml-org/llama.cpp` fix in PR #32704. This requires downloading another
GGUF with different metadata and tensor layout, so the Unsloth prebuilt
is less disruptive when the root GGUF is already cached.

References:
- https://unsloth.ai/docs/models/inkling-2-mini#run-inkling-2-mini-in-llama-cpp
- https://huggingface.co/unsloth/Inkling-2-Mini-GGUF/blob/main/Mainline_Support/README.md

## Recover from OpenCode compaction failures

If SAD logs `compaction.failed` / `Compaction summary reached the output token limit`,
the model's tool calls may already be working: OpenCode failed while summarizing
its conversation, not because of a compiler failure or a wall-clock timeout.

The original local provider advertised an 8K output allowance and reserved
65% of a 16K window for compaction. This branch uses context-scaled output
and a 15% buffer at 64K+, or 20% for smaller contexts, with up to 15K retained
recent tokens. The high-effort launcher automatically configures those values
from the real llama.cpp server.

On `compaction.failed`, SAD restarts the model in a fresh session while
preserving existing C++ source, checker results, SQLite attempt history and
investigation evidence. By default, it retries the same attempt up to three
times (the launcher allows four). If repeated compaction still fails, only
that function is deferred, rather than shutting down the whole worker.

No compaction strategy is lossless. The agent periodically writes concise,
verified hypotheses to persistent investigation files, so new sessions do not
depend on a perfect summary.

`doctor --provider local --live` verifies the configured context against
llama.cpp's actual `/v1/models` `meta.n_ctx`.

## High-effort local profile

The local backend has a separate `spore_local_high_effort_prompt.txt` instead
of inheriting the paid first-pass prompt's single-check constraint. Local
continuation passes also remove the paid-provider three-check cap. By default
the agent can make up to **24 meaningful compiler-check iterations** for a
single function and must work from retail-assembly differences. Override via
`SPORE_LOCAL_CHECK_BUDGET` (1–128).

The model's default reasoning effort is **high (0.9)**. SAD explicitly requests
`reasoning_effort=high` through OpenCode's compatible request body. Set
`SPORE_LOCAL_REASONING_EFFORT=xhigh` to ask the model for 0.99, but runtime
support for this request field depends on the installed llama.cpp build.
The local backend does not accept low/medium effort in this profile.

The runtime's active context is authoritative. SAD's output allowance scales
automatically with its declared context: 16K→4K, 32K→8K, 64K→16K, 128K→32K,
256K→64K. `SPORE_LOCAL_OUTPUT` can override within validated limits.
At 64K+ SAD uses a 15% OpenCode compaction buffer and retains up to 15K recent
tokens; the small-context buffer is 20%. It will recover up to three times
from `compaction.failed` by starting a new agent in the SAME pass using the
existing source, checker experiments and durable evidence. This does not
tick a new attempt. `SPORE_LOCAL_COMPACTION_RESTARTS` adjusts the retry cap
(0–10). Repeated compaction failure defers just that function, allowing the
worker to continue.

The model may loop or hallucinate; repeated compiler checks and an independent
final byte-exact check, not self-reported success, determine MATCH.

## Full-queue 24-hour experiment

The launcher runs a **24-hour experiment** across the existing SQLite
queue (including unclassified module functions), using the benchmark-selected
number of simultaneous OpenCode workers and llama.cpp model slots. Each function receives at most
**30 model tool calls**, counting calls across compaction recovery sessions
and repeated passes in this run. After the limit, the independent checker and
bounded proof mechanism record verified MATCH/EQUIV or a deferred candidate.

```bash
cd ~/spore-agentic-decompiler
git pull --ff-only origin dev-local-inkling
python3 scripts/run_local_high_effort.py --workers auto --max-tools 30 --hours 24
```

Stop an existing SAD coordinator before launching this. Stop a pre-existing
single-slot llama.cpp server so the launcher can start the measured
multi-slot configuration. It reads actual per-slot context from `/slots`,
not total model context. The benchmark fixes **32K per slot**, so a measured
8-slot configuration uses 256K total context. Per-worker throughput may
decrease even when aggregate throughput improves. The 24-hour deadline stops inference, allows bounded independent
verification, and preserves SQLite history and candidate files.

At any point, view results for the latest parallel run:

```bash
cd ~/spore-agentic-decompiler
python3 scripts/report_local_experiment.py
```

The report counts new byte-exact MATCH and independently proven EQUIV
separately. Previously recorded equivalence proofs are excluded from the
agent queue without being relabelled as byte-exact matches.

## Configure and test SAD from WSL2

Stop existing workers. From the SAD checkout:

```bash
git fetch origin
git switch dev-local-inkling
git pull --ff-only origin dev-local-inkling
python3 spore_agentic.py install --repo ~/spore-decomp
```

For a fresh installation, `setup --provider local --repo ~/spore-decomp`
does not require an OpenRouter key. Existing SQLite state is preserved.

Launch SAD and the inference server in the same WSL2 host/network namespace.
The model ID must match the actual `GET /v1/models` response:

```bash
export SPORE_LOCAL_BASE_URL=http://127.0.0.1:8080/v1
curl -fsS "$SPORE_LOCAL_BASE_URL/models" | python3 -m json.tool
export SPORE_LOCAL_MODEL="$(curl -fsS "$SPORE_LOCAL_BASE_URL/models" |
  python3 -c 'import sys,json; print(json.load(sys.stdin)["data"][0]["id"])')"

python3 spore_agentic.py doctor --provider local --live
python3 spore_agentic.py run --provider local --model-policy fixed \
  --workers 2 --max-inflight 1 --passes 1 --max-functions-per-worker 1
```

For a compatible vLLM server, set `SPORE_LOCAL_BASE_URL` to
`http://127.0.0.1:8000/v1` and use the model ID returned by that server.

## Dashboard and concurrency

The dashboard controller accepts `local` without paid-provider preflight;
its React source includes the Local provider option. Regenerate checked-in
frontend assets with `cd dashboard_web && npm ci && npm run build` before using
the dashboard selector, then restart the dashboard with both variables exported.

All workers share one inference server. SAD enforces `--max-inflight` through
the existing SQLite inference-slot mechanism; server batching and `--parallel`
must also be sized appropriately. Begin with one inference slot and compare exact matches
per hour, then increase concurrency.

Local mode deliberately has no automatic paid-provider fallback. Its USD budget
field is unused. OpenCode's shell tools still run unsandboxed. `doctor --live`
validates model discovery, not tool-call support, latency or match quality.
