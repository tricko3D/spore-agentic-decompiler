# Local inference with Inkling 2 Mini

SAD's optional `--provider local` uses an OpenAI-compatible server directly through
OpenCode. It does **not** send requests through OpenRouter or DeepInfra, check
their paid balances, or reserve a USD inference budget. Existing paid backends
remain unchanged.

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
