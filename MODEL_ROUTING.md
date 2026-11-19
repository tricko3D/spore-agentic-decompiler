# SAD cost-aware model routing

SAD supports direct DeepInfra and OpenRouter as independent inference providers.
Model selection is pinned **per function/pass**, never changed during retries.
The default remains the previous OpenRouter/DeepSeek configuration until a
different provider or model policy is explicitly selected.

## Supported modes

| Provider | Policy | Behavior |
| --- | --- | --- |
| OpenRouter | fixed | Existing DeepSeek V4.1 Flash 1017 and capped approved endpoints |
| OpenRouter | cheapest | Fetch public OpenRouter prices, shortlist the 10 cheapest eligible tool models and select using estimated per-pass costs |
| DeepInfra direct | fixed | DeepSeek V4.1 Flash 1017 through DeepInfra's own OpenAI-compatible endpoint and API key |

DeepInfra is **not** priced using the OpenRouter catalog. Cross-provider
pricing comparison is not enabled until independent dynamic DeepInfra pricing
can be verified. There is no automatic provider failover.

OpenRouter model discovery uses the public `/api/v1/models` endpoint. The
candidate filter requires text input/output, tools, at least 65,536 context
tokens, at least 8,192 output tokens and valid non-free/conditional pricing.
The catalogue refreshes every six hours. A stale cache of at most 24 hours can
be reused during catalog outages. If no trustworthy catalogue is available,
price-based selection fails before making paid requests.

Input, output, cache-read, cache-write and per-request costs are included in
the estimate. Initially usage is modeled as 50k input / 8k output tokens.
After at least five recorded passes, the router uses observed SAD token
volumes. The top 10 are determined by that estimate, not the provider's simple
unweighted headline price. Verified MATCH outcomes are tracked by
provider/model, though the reliability adjustment only affects ranking after
20 observed verdicts for a model. These are **observational correlations**,
not controlled estimates of model quality.

## Install, preview and launch

```bash
cd ~/spore-agentic-decompiler
git pull --ff-only origin main
python3 spore_agentic.py install --repo ~/spore-decomp
python3 -m unittest discover -s tests -p 'test_model_router.py' -v
python3 spore_agentic.py models --repo ~/spore-decomp
```

To run a small DeepInfra pilot, set `DEEPINFRA_API_KEY` in the local
environment, then:

```bash
python3 spore_agentic.py run --repo ~/spore-decomp \
  --provider deepinfra --model-policy fixed \
  --workers 2 --max-inflight 2 --max-functions-per-worker 1 --budget 1
```

To try OpenRouter's dynamic catalogue:

```bash
python3 spore_agentic.py run --repo ~/spore-decomp \
  --provider openrouter --model-policy cheapest \
  --workers 2 --max-inflight 2 --max-functions-per-worker 1 --budget 1
```

Both are **paid** operations. Start small, verify the model output, and only
then increase workers/in-flight concurrency. The CLI accepts
`SPORE_INFERENCE_PROVIDER` and `SPORE_MODEL_POLICY` environmental defaults.
The dashboard provides equivalent Provider and Model policy selectors after
the frontend is rebuilt:

```bash
cd ~/spore-agentic-decompiler/dashboard_web
npm ci
npm run build
cd ..
python3 spore_agentic.py dashboard --control
```

The dashboard process must inherit `DEEPINFRA_API_KEY` if DeepInfra is to be
started from the UI. No API key is written into repository config or JSON logs.

## Spending guard and caveats

OpenRouter retains its authenticated `/key` allowance and original SQLite
history. DeepInfra uses its own `/payment/usage?from=current` billing baseline
and a separate key fingerprint in SQLite. A DeepInfra API key needs
`billing:read` permission. If usage cannot be verified, no paid inference
is started. Direct-provider billing history never rotates/resets the
OpenRouter key baseline.

The DeepInfra per-run budget is a **soft cap**. Provider billing data can lag
live inference, so an already-running call or recently unreported usage can
overshoot the cap. Configure a separate spending limit in the DeepInfra
account for protection against overspending.

Price estimates do not guarantee routing endpoint availability or output
quality. Cost-per-MATCH requires controlled comparisons across equivalent
function difficulty and a much larger sample. The router currently records
actual OpenCode step-finish cost and token telemetry when present; missing
usage is left unknown rather than treated as zero.

All existing attempts, manifests, C++ edits, prompt customizations and Brain
knowledge are preserved. The installer creates backups before overwriting any
changed engine scripts.
