#!/usr/bin/env python3
"""High-effort local SAD runner: use benchmark-selected llama.cpp configuration.

Stdlib-only. Reuses an existing loopback server or starts the locally installed
Unsloth binary and cached GGUF. Never contacts paid inference services.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
SERVER = Path.home() / "inkling-runtime" / "llama.cpp" / "llama-server"
CACHE = Path.home() / ".cache/huggingface/hub/models--unsloth--Inkling-2-Mini-GGUF/snapshots"
OVERRIDE = (
    r"blk\.(0|1|2|3|4|5|18|19|20|21|22|23)\.ffn_.*_exps\.weight=CPU"
)
CONTEXT_STEPS = (131072, 65536, 32768, 16384)
PROFILE = Path.home() / "inkling-runtime" / "sad-parallel-profile.json"


def expert_override(cpu_expert_layers: int = 12) -> str:
    if cpu_expert_layers not in (8, 10, 12, 14, 16, 18):
        raise ValueError("CPU expert layers must be one of 8, 10, 12, 14, 16, 18")
    half = cpu_expert_layers // 2
    indices = list(range(half)) + list(range(24 - half, 24))
    return r"blk\.(" + "|".join(map(str, indices)) + r")\.ffn_.*_exps\.weight=CPU"


def query_server(base: str, timeout: float = 3):
    try:
        with urlopen(Request(base + "/models", headers={"Accept": "application/json"}),
                     timeout=timeout) as response:
            obj = json.load(response)
        entries = obj.get("data", []) if isinstance(obj, dict) else []
        if entries and isinstance(entries[0], dict) and entries[0].get("id"):
            return entries[0]
    except (OSError, ValueError, TimeoutError, URLError):
        pass
    return None


def query_slots(base: str):
    """Read true per-agent context and available slots from llama.cpp."""
    url = base.removesuffix("/v1") + "/slots"
    try:
        with urlopen(Request(url, headers={"Accept": "application/json"}),
                     timeout=5) as response:
            data = json.load(response)
    except (OSError, ValueError, TimeoutError, URLError) as exc:
        raise RuntimeError("Cannot inspect llama.cpp /slots; restart server with this launcher") from exc
    if not isinstance(data, list) or not data:
        raise RuntimeError("llama.cpp /slots did not return an active slot list")
    windows = [s.get("n_ctx") for s in data if isinstance(s, dict)]
    if len(windows) != len(data) or any(type(n) is not int or n < 8192 for n in windows):
        raise RuntimeError("llama.cpp /slots did not return valid per-slot contexts")
    return len(data), min(windows)


def cached_model() -> Path:
    matches = list(CACHE.glob("*/Inkling-2-Mini-UD-Q4_K_M.gguf"))
    matches = [p for p in matches if p.is_file() and p.stat().st_size > 1000000000]
    if not matches:
        raise RuntimeError("Cached Inkling Q4_K_M GGUF not found in " + str(CACHE))
    return max(matches, key=lambda p: p.stat().st_mtime)


def contexts(maximum: int):
    """Attempt the user's requested context once, then decreasing known sizes."""
    return list(dict.fromkeys([maximum] + [c for c in CONTEXT_STEPS if c < maximum]))


def start_server(max_context: int, base: str, slots: int = 2, threads: int = 6,
                 cpu_expert_layers: int = 12, allow_context_fallback: bool = True,
                 startup_seconds: int = 240):
    """Start measured GPU/CPU offload. Exact context during benchmarking."""
    if slots < 1 or not 1 <= threads <= 32:
        raise ValueError("slots must be positive and CPU threads must be 1..32")
    offload = expert_override(cpu_expert_layers)
    if base != "http://127.0.0.1:8080/v1":
        raise RuntimeError("Automatic server startup is supported at http://127.0.0.1:8080/v1")
    if not SERVER.is_file():
        raise RuntimeError("Unsloth llama-server binary not found at " + str(SERVER))
    model = cached_model()
    log_dir = Path.home() / "inkling-runtime"
    log_dir.mkdir(parents=True, exist_ok=True)
    candidates = contexts(max_context) if allow_context_fallback else [max_context]
    for context in candidates:
        log = log_dir / f"sad-inkling-{context}-np{slots}-t{threads}-cpu{cpu_expert_layers}.log"
        command = [
            str(SERVER), "--model", str(model),
            "--host", "127.0.0.1", "--port", "8080",
            "--jinja", "-c", str(context), "-ngl", "99",
            "--fit", "off", "-fa", "on", "-np", str(slots), "-t", str(threads),
            "-b", "1024", "-ub", "32" if context >= 131072 else "64",
            "-ctk", "q4_0", "-ctv", "q4_0",
            "--temp", "1.0", "--top-p", "1.0", "--top-k", "0",
            "--min-p", "0.0", "-ot", offload,
        ]
        print(f"Starting optimized Inkling at {context:,} tokens, log: {log}", flush=True)
        with log.open("wb") as stream:
            proc = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                    cwd=log_dir, start_new_session=True)
        deadline = time.monotonic() + startup_seconds
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            served = query_server(base)
            if served is not None:
                return proc, served
            time.sleep(2)
        stop_server(proc)
        try:
            tail = "\n".join(log.read_text(errors="replace").splitlines()[-8:])
        except OSError:
            tail = "(no log)"
        print(f"Inkling failed to start at {context:,} tokens:\n{tail}\n", file=sys.stderr,
              flush=True)
    raise RuntimeError("Inkling failed at every context size. Check the server logs above.")


def stop_server(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=20)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(Path.home() / "spore-decomp"))
    parser.add_argument("--context", type=int, default=131072,
                        help="Preferred server context, falls back to smaller windows on OOM")
    parser.add_argument("--output", type=int, default=0,
                        help="Max output tokens per call, 0 automatically uses context/4")
    parser.add_argument("--checks", type=int, default=12,
                        help="Evidence-guided compiler-check cycles per function (1-128)")
    parser.add_argument("--reasoning-effort", choices=("high", "xhigh", "max"),
                        default="xhigh")
    parser.add_argument("--minutes", type=int, default=60,
                        help="Inactivity watchdog, not a total agent timeout")
    parser.add_argument("--workers", default="auto",
                        help="auto uses measured benchmark profile; alternatively any positive integer")
    parser.add_argument("--benchmark-profile", default=str(PROFILE),
                        help="Saved results from scripts/benchmark_local_parallel.py")
    parser.add_argument("--threads", type=int, default=6,
                        help="CPU threads for llama.cpp when workers are manual")
    parser.add_argument("--cpu-expert-layers", type=int, default=12,
                        help="MoE expert layers on CPU (manual mode)")
    parser.add_argument("--max-tools", type=int, default=30,
                        help="Absolute tool-call cap per function across retries")
    parser.add_argument("--hours", type=float, default=24,
                        help="Total experiment duration in hours")
    parser.add_argument("--max-functions-per-worker", type=int, default=0)
    parser.add_argument("--passes", type=int, default=1,
                        help="Additional full attempts per eligible function")
    parser.add_argument("--serve-only", action="store_true",
                        help="Start optimized local inference and keep it running")
    args = parser.parse_args(argv)
    if args.context < 16384 or args.context > 1048576:
        parser.error("--context must be between 16384 and 1048576")
    if args.output < 0 or args.checks not in range(1, 129) or not 1 <= args.max_tools <= 10000:
        parser.error("--output nonnegative, --checks 1..128, --max-tools 1..10000")
    if not 0 < args.hours <= 168:
        parser.error("--hours must be between 0 and 168")
    auto = args.workers == "auto"
    if auto:
        try:
            selected = json.loads(Path(args.benchmark_profile).expanduser().read_text())["recommended"]
            args.workers = int(selected["workers"])
            args.context = int(selected["total_context"])
            args.threads = int(selected["threads"])
            args.cpu_expert_layers = int(selected["cpu_expert_layers"])
            required_slot_context = int(selected["slot_context"])
        except (OSError, ValueError, TypeError, KeyError) as exc:
            parser.error("No usable throughput profile. Run "
                         "python3 scripts/benchmark_local_parallel.py first. "
                         f"Details: {exc}")
        print("Using measured benchmark profile:", args.benchmark_profile, flush=True)
    else:
        try:
            args.workers = int(args.workers)
        except ValueError:
            parser.error("--workers must be auto or a positive integer")
        required_slot_context = 0
    if args.workers < 1:
        parser.error("--workers must be positive")
    if not 1 <= args.threads <= 32:
        parser.error("--threads must be 1..32")
    try:
        expert_override(args.cpu_expert_layers)
    except ValueError as exc:
        parser.error(str(exc))
    if args.minutes < 1 or args.minutes > 120:
        parser.error("--minutes must be 1..120")
    if args.passes < 1 or args.passes > 24:
        parser.error("--passes must be 1..24")
    base = os.environ.get("SPORE_LOCAL_BASE_URL", "http://127.0.0.1:8080/v1").rstrip("/")
    if base != "http://127.0.0.1:8080/v1":
        parser.error("This launcher requires http://127.0.0.1:8080/v1")
    owned = None
    try:
        served = query_server(base)
        if served is None:
            owned, served = start_server(
                args.context, base, args.workers,
                threads=args.threads, cpu_expert_layers=args.cpu_expert_layers,
                allow_context_fallback=not auto)
        else:
            print("Reusing the already-running Inkling server.", flush=True)
        model_id = served["id"]
        if auto and selected.get("model_id") and selected["model_id"] != model_id:
            raise RuntimeError("The model ID differs from the benchmark profile. "
                               "Re-run scripts/benchmark_local_parallel.py.")
        available_slots, context = query_slots(base)
        if available_slots < args.workers:
            raise RuntimeError(
                f"Existing llama.cpp server has only {available_slots} slot(s), "
                f"but {args.workers} workers requested. Stop the old server "
                "and rerun this launcher to start multiple slots.")
        if auto and (available_slots != args.workers or context < required_slot_context):
            raise RuntimeError(
                f"Active llama.cpp server has {available_slots} slots with {context} context "
                f"per slot, but the benchmark selected {args.workers} slots with at least "
                f"{required_slot_context} context. Stop the old server first.")
        output = args.output or min(65536, max(4096, context // 4))
        if output > min(65536, context // 2):
            raise RuntimeError(f"Output {output} does not fit available {context} context")
        env = os.environ.copy()
        env.update({
            "SPORE_LOCAL_BASE_URL": base,
            "SPORE_LOCAL_MODEL": model_id,
            "SPORE_LOCAL_CONTEXT": str(context),
            "SPORE_LOCAL_OUTPUT": str(output),
            "SPORE_LOCAL_CHECK_BUDGET": str(args.checks),
            "SPORE_LOCAL_MAX_TOOLS": str(args.max_tools),
            "SPORE_RUN_DEADLINE_AT": str(time.time() + args.hours * 3600),
            "SPORE_ALL_QUEUED": "1",
            "SPORE_LOCAL_REASONING_EFFORT": args.reasoning_effort,
            "SPORE_LOCAL_COMPACTION_RESTARTS": "4",
        })
        env.pop("SPORE_LOCAL_FRESH_SESSION", None)
        print(f"High-effort Inkling: context={context:,}, output={output:,}, "
              f"checks/function={args.checks}, tools/function={args.max_tools}, "
              f"workers={args.workers}, slots={available_slots}, "
              f"CPU threads={args.threads}, CPU expert layers={args.cpu_expert_layers}, "
              f"duration={args.hours:g}h, effort={args.reasoning_effort}", flush=True)
        if args.serve_only:
            if owned is None:
                print("Server already running; no child process to manage.")
                return 0
            print("Server ready. Press Ctrl+C to stop.")
            while owned.poll() is None:
                time.sleep(5)
            return owned.returncode
        command = [sys.executable, str(ROOT / "spore_agentic.py"),
                   "install", "--repo", str(Path(args.repo).expanduser())]
        if subprocess.run(command, cwd=ROOT, env=env).returncode != 0:
            return 1
        doctor = [sys.executable, str(ROOT / "spore_agentic.py"),
                  "doctor", "--repo", str(Path(args.repo).expanduser()),
                  "--provider", "local", "--live"]
        if subprocess.run(doctor, cwd=ROOT, env=env).returncode != 0:
            return 1
        run = [sys.executable, str(ROOT / "spore_agentic.py"), "run",
               "--repo", str(Path(args.repo).expanduser()),
               "--provider", "local", "--model-policy", "fixed",
               "--workers", str(args.workers), "--max-inflight", str(args.workers),
               "--fixed-inflight", "--passes", str(args.passes),
               "--minutes", str(args.minutes), "--all-modules"]
        if args.max_functions_per_worker:
            run += ["--max-functions-per-worker", str(args.max_functions_per_worker)]
        print("Running all eligible SQLite queue functions for the experiment.", flush=True)
        rc = subprocess.run(run, cwd=ROOT, env=env).returncode
        print("\nVerified experiment outcomes:", flush=True)
        subprocess.run([sys.executable, str(ROOT / "scripts/report_local_experiment.py"),
                        "--repo", str(Path(args.repo).expanduser())],
                       cwd=ROOT, env=env, check=False)
        return rc
    except KeyboardInterrupt:
        print("\nStopping local SAD launcher.", flush=True)
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Local SAD launcher: {exc}", file=sys.stderr)
        return 1
    finally:
        stop_server(owned)


if __name__ == "__main__":
    sys.exit(main())
