"""Cost-aware model selection for short-lived Spore agents, stdlib only.

Prices are USD per token in OpenRouter's public catalog, not USD per million.
No model is accepted without text I/O, tool calls, context and valid pricing.
Existing model choices are pinned for a VA/pass across provider retries.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

CATALOG_URL = "https://openrouter.ai/api/v1/models?supported_parameters=tools"
DEEPINFRA_URL = "https://api.deepinfra.com/v1/openai"
DEEPINFRA_MODEL = "deepseek-ai/DeepSeek-V4.1-Flash-1017"
CACHE_TTL = 6 * 3600
MAX_STALE = 24 * 3600
EXPECTED_INPUT = 50_000
EXPECTED_OUTPUT = 8_000
CACHED_READ_FRACTION = 0.25
CACHED_WRITE_FRACTION = 0.05
MIN_CONTEXT = 65_536
MIN_OUTPUT = 8_192


def _money(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("missing or invalid model price") from None
    if not math.isfinite(result) or result < 0:
        raise ValueError("negative or nonfinite model price")
    return result


def estimate(pricing, *, input_tokens=EXPECTED_INPUT, output_tokens=EXPECTED_OUTPUT,
             cached_fraction=CACHED_READ_FRACTION):
    """Pricing input is OpenRouter USD/token, not USD/million."""
    if not 0 <= cached_fraction <= 1:
        raise ValueError("Invalid cache fraction")
    fresh = _money(pricing["prompt"])
    out = _money(pricing["completion"])
    cached = _money(pricing.get("input_cache_read", fresh))
    # Only credit cache hits if the provider advertises a read price.
    if "input_cache_read" not in pricing:
        cached_fraction = 0
    writing = _money(pricing.get("input_cache_write", fresh))
    write_fraction = CACHED_WRITE_FRACTION if "input_cache_write" in pricing else 0
    if cached_fraction + write_fraction > 1:
        raise ValueError("cache fractions exceed all input")
    request = _money(pricing.get("request", 0))
    return (input_tokens * (1 - cached_fraction - write_fraction) * fresh +
            input_tokens * cached_fraction * cached +
            input_tokens * write_fraction * writing +
            output_tokens * out + request)


def parse_catalog(data, *, input_tokens=EXPECTED_INPUT, output_tokens=EXPECTED_OUTPUT):
    entries = data.get("data") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError("OpenRouter catalog response missing data")
    eligible = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ident = entry.get("id")
        if not isinstance(ident, str) or "/" not in ident or ident.endswith(":free"):
            continue
        if not isinstance(entry.get("supported_parameters"), list) or "tools" not in entry["supported_parameters"]:
            continue
        arch = entry.get("architecture") or {}
        if not isinstance(arch, dict):
            continue
        if "text" not in (arch.get("input_modalities") or []) or "text" not in (arch.get("output_modalities") or []):
            continue
        context = entry.get("context_length")
        top = entry.get("top_provider") or {}
        if not isinstance(top, dict):
            top = {}
        output = top.get("max_completion_tokens") or MIN_OUTPUT
        if type(context) is not int or context < MIN_CONTEXT or type(output) is not int or output < MIN_OUTPUT:
            continue
        pricing = entry.get("pricing")
        if not isinstance(pricing, dict) or pricing.get("overrides"):
            # Conditional long-context/time-dependent prices need a separate
            # implementation; don't understate their cost.
            continue
        try:
            cost = estimate(pricing, input_tokens=input_tokens, output_tokens=output_tokens)
        except (KeyError, ValueError):
            continue
        if cost <= 0:
            # Free endpoints are frequently throttled and not meaningful
            # comparators for a paid, sustained decompilation workload.
            continue
        eligible.append({
            "provider": "openrouter",
            "model": ident,
            "name": str(entry.get("name") or ident),
            "context": context,
            "max_output": output,
            "estimate_usd": round(cost, 9),
            "prompt_per_m": _money(pricing["prompt"]) * 1_000_000,
            "completion_per_m": _money(pricing["completion"]) * 1_000_000,
            "cache_read_per_m": (_money(pricing["input_cache_read"]) * 1_000_000
                                 if "input_cache_read" in pricing else None),
            "cache_write_per_m": (_money(pricing["input_cache_write"]) * 1_000_000
                                  if "input_cache_write" in pricing else None),
        })
    eligible.sort(key=lambda x: (x["estimate_usd"], x["model"]))
    return eligible


def catalogue(cache_path, *, refresh=False, now=None):
    """Fetch live OpenRouter prices with atomic 6h cache and 24h fail-closed stale bound."""
    cache_path = Path(cache_path)
    now = time.time() if now is None else now
    stored = None
    try:
        stored = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(stored, dict) or not isinstance(stored.get("data"), list):
            stored = None
    except (OSError, ValueError):
        pass
    age = now - cache_path.stat().st_mtime if stored is not None else float("inf")
    if stored is not None and 0 <= age <= CACHE_TTL and not refresh:
        return stored
    try:
        req = Request(CATALOG_URL, headers={"Accept": "application/json", "User-Agent": "SAD-ModelRouter/1"})
        with urlopen(req, timeout=15) as response:
            fresh = json.load(response)
        if not isinstance(fresh, dict) or not isinstance(fresh.get("data"), list):
            raise ValueError("Invalid OpenRouter catalog data")
        if not parse_catalog(fresh):
            raise ValueError("No eligible priced tool-calling models in catalog")
    except (OSError, ValueError, TimeoutError) as exc:
        if stored is not None and 0 <= age <= MAX_STALE and not refresh:
            return stored
        raise RuntimeError("OpenRouter prices unavailable; refusing unpriced routing: " + type(exc).__name__) from None
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp = cache_path.with_name(cache_path.name + "." + str(os.getpid()) + ".tmp")
    try:
        temp.write_text(json.dumps(fresh, separators=(",", ":")), encoding="utf-8")
        os.replace(temp, cache_path)
    finally:
        temp.unlink(missing_ok=True)
    return fresh


def top_ten(cache_path, *, refresh=False, input_tokens=EXPECTED_INPUT,
            output_tokens=EXPECTED_OUTPUT):
    return parse_catalog(catalogue(cache_path, refresh=refresh),
                         input_tokens=input_tokens, output_tokens=output_tokens)[:10]


def ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS model_passes (
        run_id TEXT NOT NULL, va TEXT NOT NULL, pass_number INTEGER NOT NULL,
        provider TEXT NOT NULL, model TEXT NOT NULL, policy TEXT NOT NULL,
        estimated_cost REAL, status TEXT, observed_cost REAL,
        input_tokens INTEGER, output_tokens INTEGER, cached_input_tokens INTEGER,
        chosen_utc TEXT NOT NULL,
        PRIMARY KEY (run_id,va,pass_number)
    )""")


def performance(db, provider, model):
    ensure_schema(db)
    row = db.execute("""SELECT COUNT(*) AS n,
        SUM(CASE WHEN status='MATCH' THEN 1 ELSE 0 END) AS matched
        FROM model_passes WHERE provider=? AND model=?
        AND status IN ('MATCH','FAIL','DEFER','SKIP')""", (provider, model)).fetchone()
    return int(row[0]), int(row[1] or 0)


def usage_profile(db):
    """Prefer observed SAD token volume once five passes have emitted usage."""
    ensure_schema(db)
    rows = db.execute("""SELECT input_tokens,output_tokens FROM model_passes
                         WHERE input_tokens>0 AND output_tokens>0
                         ORDER BY chosen_utc DESC LIMIT 80""").fetchall()
    if len(rows) < 5:
        return EXPECTED_INPUT,EXPECTED_OUTPUT
    return (max(1000,round(sum(x[0] for x in rows)/len(rows))),
            max(1000,round(sum(x[1] for x in rows)/len(rows))))


def score(candidate, db):
    """No model-quality inference from tiny observational samples."""
    n, match = performance(db, candidate["provider"], candidate["model"])
    if n < 20:
        return candidate["estimate_usd"]
    # Beta(2,2) shrinkage. Not causal evidence; observational signal only.
    posterior = (match + 2) / (n + 4)
    return candidate["estimate_usd"] / max(0.1, posterior)


def choose(db, *, run_id, va, pass_number, provider="openrouter",
           policy="fixed", cache_path=None, fixed_openrouter="deepseek/deepseek-v4.1-flash-1017"):
    """Atomically pin one model for this function/pass across retries."""
    if provider not in ("openrouter", "deepinfra", "local") or policy not in ("fixed", "cheapest"):
        raise ValueError("Unsupported provider/model policy")
    ensure_schema(db)
    existing = db.execute("""SELECT provider,model,policy,estimated_cost FROM model_passes
                             WHERE run_id=? AND va=? AND pass_number=?""",
                          (run_id, va, pass_number)).fetchone()
    if existing:
        if existing[0] != provider:
            raise RuntimeError("Provider changed during an already-started pass")
        return {"provider": existing[0], "model": existing[1], "policy": existing[2],
                "estimate_usd": existing[3]}
    if provider == "local":
        if policy != "fixed":
            raise ValueError("Local inference uses fixed-model routing only")
        _, local_id = local_settings()
        selected = {"provider": provider, "model": local_id, "estimate_usd": 0.0}
    elif provider == "deepinfra":
        if policy != "fixed":
            raise ValueError("DeepInfra prices are not in the OpenRouter catalog; use fixed policy")
        selected = {"provider": provider, "model": DEEPINFRA_MODEL, "estimate_usd": None}
    elif policy == "fixed":
        selected = {"provider": provider, "model": fixed_openrouter, "estimate_usd": None}
    else:
        if cache_path is None:
            raise ValueError("Price cache path required")
        expected_input,expected_output = usage_profile(db)
        candidates = top_ten(cache_path,input_tokens=expected_input,
                             output_tokens=expected_output)
        if not candidates:
            raise RuntimeError("No eligible models in refreshed price catalog")
        selected = min(candidates, key=lambda x: (score(x, db), x["estimate_usd"], x["model"]))
    db.execute("""INSERT OR IGNORE INTO model_passes
      (run_id,va,pass_number,provider,model,policy,estimated_cost,chosen_utc)
      VALUES(?,?,?,?,?,?,?,datetime('now'))""",
      (run_id, va, pass_number, provider, selected["model"], policy, selected["estimate_usd"]))
    return {"provider": provider, "model": selected["model"], "policy": policy,
            "estimate_usd": selected["estimate_usd"]}


def record(db, *, run_id, va, pass_number, status, log):
    """Observational metadata only, called after independent checker verdict."""
    if status not in ("MATCH", "FAIL", "DEFER", "SKIP"):
        return
    cost = 0.
    inp = out = cached = 0
    steps = 0
    try:
        with Path(log).open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict) or event.get("type") != "step_finish":
                    continue
                part = event.get("part") or {}
                if not isinstance(part, dict):
                    continue
                value = part.get("cost")
                if isinstance(value, (int,float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value < 10000:
                    cost += value
                    steps += 1
                tokens = part.get("tokens") or {}
                if isinstance(tokens, dict):
                    inp += int(tokens.get("input") or 0)
                    out += int(tokens.get("output") or 0)
                    cached += int((tokens.get("cache") or {}).get("read") or 0)
    except (OSError, ValueError, TypeError, OverflowError):
        pass
    ensure_schema(db)
    db.execute("""UPDATE model_passes SET status=?,observed_cost=?,
        input_tokens=?,output_tokens=?,cached_input_tokens=?
        WHERE run_id=? AND va=? AND pass_number=?""",
        (status, round(cost, 9) if steps else None, inp, out, cached, run_id, va, pass_number))


def deepinfra_month_spend(key):
    """Read DeepInfra's billed current-month total. No inference is purchased."""
    req = Request("https://api.deepinfra.com/payment/usage?from=current",
                  headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=20) as response:
            body = json.load(response)
    except Exception as exc:
        raise RuntimeError("DeepInfra billing:read access required for safe run budgets (" +
                           type(exc).__name__ + ")") from None
    months = body.get("months") if isinstance(body, dict) else None
    if not isinstance(months, list):
        raise RuntimeError("DeepInfra response lacks a monthly usage array")
    if not months:
        if isinstance(body.get("initial_month"), str):
            return 0.0
        raise RuntimeError("DeepInfra returned empty usage without an initial_month marker")
    # The endpoint is requested for 'current' only. Sum explicit cost fields
    # rather than interpreting invoice balances or token counts as dollars.
    try:
        return sum(_money(month["total_cost"]) for month in months)
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("DeepInfra billing response missing numeric total_cost") from None


def direct_opencode_config(model, reasoning):
    """Ephemeral provider settings. The key stays in DEEPINFRA_API_KEY."""
    return {
        "$schema": "https://opencode.ai/config.json",
        "share": "disabled",
        "compaction": {"auto": True, "keep": {"tokens": 2000}, "buffer": "65%"},
        "model": "spore_deepinfra/" + model,
        "providers": {
            "spore_deepinfra": {
                "name": "DeepInfra direct (SAD)",
                "env": ["DEEPINFRA_API_KEY"],
                "package": "@opencode/ai/providers/openai-compatible",
                "settings": {"baseURL": DEEPINFRA_URL},
                "models": {
                    model: {
                        "modelID": model, "name": model,
                        "limit": {"context": 131072, "output": 32768},
                        "capabilities": {"tools": True, "input": ["text"], "output": ["text"]},
                    },
                },
            },
        },
        "permissions": [
            {"action": "shell", "resource": "git commit *", "effect": "deny"},
            {"action": "shell", "resource": "git push *", "effect": "deny"},
            {"action": "shell", "resource": "git reset --hard *", "effect": "deny"},
        ],
    }


def local_endpoint(settings=None):
    """Validate the configured local server before making any HTTP request."""
    env = os.environ if settings is None else settings
    endpoint = env.get("SPORE_LOCAL_BASE_URL", "http://127.0.0.1:8080/v1").rstrip("/")
    url = urlsplit(endpoint)
    if (url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost", "::1")
            or url.path != "/v1" or url.query or url.fragment or url.username or url.password):
        raise ValueError("SPORE_LOCAL_BASE_URL must be an HTTP loopback /v1 endpoint")
    return endpoint


def local_settings(settings=None):
    """Resolve the loopback-only endpoint and exact running model ID."""
    env = os.environ if settings is None else settings
    endpoint = local_endpoint(env)
    model = env.get("SPORE_LOCAL_MODEL", "").strip()
    if not model or any(ord(ch) < 32 for ch in model):
        raise ValueError("SPORE_LOCAL_MODEL must be the exact ID from GET /v1/models")
    return endpoint, model


def local_limits(settings=None):
    """Match the actual llama.cpp context window; leave room for tool replies."""
    env = os.environ if settings is None else settings
    try:
        context = int(env.get("SPORE_LOCAL_CONTEXT", "16384"))
        raw_output = env.get("SPORE_LOCAL_OUTPUT", "").strip()
        output = int(raw_output) if raw_output else min(65536, max(4096, context // 4))
    except ValueError:
        raise ValueError("SPORE_LOCAL_CONTEXT and SPORE_LOCAL_OUTPUT must be integers") from None
    if not 8192 <= context <= 1048576 or not 512 <= output <= min(65536, context // 2):
        raise ValueError("Invalid local context/output limits (output must fit within context)")
    return context, output


def probe_local(settings=None):
    """Unpaid discovery preflight; no inference or OpenRouter calls."""
    endpoint, model = local_settings(settings)
    context, _ = local_limits(settings)
    try:
        with urlopen(Request(endpoint + "/models", headers={"Accept": "application/json"}), timeout=5) as reply:
            payload = json.load(reply)
    except (OSError, ValueError) as exc:
        raise RuntimeError("Local model endpoint is unavailable: " + type(exc).__name__) from None
    models = payload.get("data") if isinstance(payload, dict) else None
    match = next((entry for entry in models if isinstance(entry, dict)
                  and entry.get("id") == model), None) if isinstance(models, list) else None
    if match is None:
        raise RuntimeError("SPORE_LOCAL_MODEL was not found in the server's /v1/models response")
    meta = match.get("meta") or {}
    server_ctx = meta.get("n_ctx") if isinstance(meta, dict) else None
    if isinstance(server_ctx, int) and not isinstance(server_ctx, bool) and context > server_ctx:
        raise RuntimeError(
            f"SPORE_LOCAL_CONTEXT={context} exceeds the server's active context {server_ctx}; "
            "restart llama.cpp with a larger -c or lower SPORE_LOCAL_CONTEXT"
        )
    return endpoint, model


def local_slot_seed(run_id, worker_idx):
    """Unique per-run local worker tag in llama.cpp's existing seed field.

    The seed has the same pseudorandom distribution as an ordinary sampling
    seed. Its value also identifies a worker in the read-only /slots telemetry,
    without adding an HTTP proxy, daemon or separate dashboard.
    """
    offset = int.from_bytes(hashlib.sha256(run_id.encode()).digest()[:4], "big")
    return (offset + worker_idx) % 4294967295


def local_opencode_config():
    """Direct OpenAI-compatible model provider; no paid routing or API key."""
    endpoint, model = local_settings()
    context, output = local_limits()
    effort = os.environ.get("SPORE_LOCAL_REASONING_EFFORT", "high").lower()
    if effort not in ("high", "xhigh", "max"):
        raise ValueError("SPORE_LOCAL_REASONING_EFFORT must be high, xhigh or max")
    body = {"reasoning_effort": effort}
    run_id = os.environ.get("SPORE_SQLITE_RUN_ID", "")
    worker_idx = os.environ.get("SPORE_SQLITE_WORKER_IDX", "")
    if run_id and worker_idx.isdecimal():
        body["seed"] = local_slot_seed(run_id, int(worker_idx))
    return {
        "$schema": "https://opencode.ai/config.json",
        "share": "disabled",
        # Retain recent verified tool/checker evidence, with room for future output.
        # The server's actual context is validated by probe_local().
        "compaction": {
            "auto": True, "background": False,
            "keep": {"tokens": min(15000, max(1200, context // 8))},
            "buffer": "15%" if context >= 65536 else "20%",
        },
        "model": "spore_local/inkling",
        "providers": {
            "spore_local": {
                "name": "Local inference (SAD)",
                "package": "@opencode/ai/providers/openai-compatible",
                "settings": {"baseURL": endpoint, "apiKey": "local"},
                "models": {
                    "inkling": {
                        "modelID": model, "name": model,
                        "limit": {"context": context, "output": output},
                        "body": body,
                        "capabilities": {"tools": True, "input": ["text"], "output": ["text"]},
                    },
                },
            },
        },
        "permissions": [
            {"action": "shell", "resource": "git commit *", "effect": "deny"},
            {"action": "shell", "resource": "git push *", "effect": "deny"},
            {"action": "shell", "resource": "git reset --hard *", "effect": "deny"},
        ],
    }
