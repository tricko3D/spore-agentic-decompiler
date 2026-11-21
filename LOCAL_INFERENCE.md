# Local inference with Inkling 2 Mini

SAD's optional `--provider local` uses an OpenAI-compatible server directly through
OpenCode. It does **not** send requests through OpenRouter or DeepInfra, check
their paid balances, or reserve a USD inference budget. Existing paid backends
remain unchanged.

## Start an inference server

Inkling 2 Mini's official vLLM implementation currently targets NVIDIA Hopper,
Blackwell and DGX Spark. The RTX 4070 Ti (Ada, 12 GB VRAM) is **not a verified
vLLM configuration**. Start with the CUDA-enabled current llama.cpp runtime and
Unsloth's GGUF, loading the rest of the weights into system RAM:

```bash
llama serve -hf unsloth/Inkling-2-Mini-GGUF:UD-Q4_K_M \
  --host 127.0.0.1 --port 8080 -c 16384 -ngl 8 --parallel 1 --jinja
```

Install CUDA llama.cpp from https://github.com/ggml-org/llama.cpp if needed.
Start with one inference slot so the full 16K context is available to it.
When increasing to two simultaneous server slots, increase `-c` to 32768
(or lower the per-request context limits) and check KV cache memory.
Increase GPU offloading only after measuring VRAM, RAM and throughput. Q4_K_M
weights are about 16.6 GB, more than 4070 Ti's 12 GB VRAM. Test tool calling,
not just text generation, before dispatching decompilation workers. If a compatible
vLLM server works on your hardware, its API can be used instead.

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
