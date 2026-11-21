#!/usr/bin/env python3
"""Find the fastest *aggregate* Inkling throughput for parallel SAD workers.

All inference is local. Each candidate runs the same representative
reverse-engineering workload with its own llama.cpp server configuration.
The benchmark measures generated tokens divided by elapsed wall-clock time;
it does not mistake per-slot speed or slot count for total throughput.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from run_local_high_effort import (PROFILE, SERVER, cached_model, query_server,
                                   query_slots, start_server, stop_server)

BASE = "http://127.0.0.1:8080/v1"
OUTPUT = Path.home() / "inkling-runtime" / "sad-parallel-benchmark.json"


def numbers(value: str, upper: int = 32):
    try:
        items = sorted(set(int(x.strip()) for x in value.split(",")))
    except ValueError:
        raise argparse.ArgumentTypeError("Expected comma-separated integers") from None
    if not items or any(x < 1 or x > upper for x in items):
        raise argparse.ArgumentTypeError(f"Values must be between 1 and {upper}")
    return items


def reverse_engineering_prompt(index: int, size_kib: int):
    """Repeat realistic assembler/compiler evidence to stress prompt prefill."""
    head = (
        "Analyze a C++ MSVC x86 retail byte-matching problem in high detail. "
        "This is a hypothetical isolated function and does not modify files. "
        "Explain the calling convention, object layout, register schedule, "
        "specific compiler levers and testable hypotheses. "
        "Produce at least 20 numbered hypotheses, each with four complete sentences. "
        "Continue with numbered hypotheses until you exhaust the output budget.\n"
        f"Experiment id {index}. Retail constructor length 57 bytes. "
        "Source allocates an inline fixed_string, copies begin and end pointers, "
        "and calls assign. Exact instruction order matters.\n"
    )
    evidence = (
        "Retail: mov esi,ecx | lea eax,[esi+0x14] | "
        "mov dword ptr [esi+0x10],eax | lea ecx,[eax+0x40] | "
        "push edi | mov edi,[esp+0xc] | mov [esi+8],ecx | "
        "mov [esi+4],eax | mov [esi],eax | xor ecx,ecx | "
        "mov word ptr [eax],cx | call assign | ret 4. "
        "Candidate: push edi and mov edi occur before capacity calculation. "
        "The _ReadWriteBarrier placement controls stores but not the EDI schedule. "
        "Compiler flags: /O2 /MD /Gy /EHsc /TP, x86 ABI.\n"
    )
    target = max(len(head), size_kib * 1024)
    chunks = [head]
    while sum(map(len, chunks)) < target:
        chunks.append(f"Evidence sample {len(chunks)}: {evidence}")
    return "".join(chunks)


def request_completion(model: str, prompt: str, n_predict: int,
                       timeout: int, request_id: int):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": n_predict,
        "temperature": 1.0,
        "top_p": 1.0,
        "reasoning_effort": "xhigh",
        "stream": False,
    }
    request = Request(
        BASE + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    with urlopen(request, timeout=timeout) as reply:
        response = json.load(reply)
    seconds = time.perf_counter() - started
    if "error" in response:
        raise RuntimeError(f"Request {request_id}: {response['error']}")
    usage = response.get("usage") or {}
    timing = response.get("timings") or {}
    output = usage.get("completion_tokens")
    if not isinstance(output, int):
        output = timing.get("predicted_n")
    input_tokens = usage.get("prompt_tokens")
    if not isinstance(input_tokens, int):
        input_tokens = timing.get("prompt_n")
    if not isinstance(output, int) or output < 16:
        raise RuntimeError(f"Request {request_id}: token usage unavailable or under 16 tokens")
    return {
        "output_tokens": output,
        "input_tokens": input_tokens if isinstance(input_tokens, int) else None,
        "latency_seconds": seconds,
        "request_id": request_id,
    }


def round_concurrency(model: str, count: int, prompt_kib: int,
                      max_output: int, timeout: int, seed: int,
                      send=request_completion):
    barrier = threading.Barrier(count + 1)

    def task(i):
        prompt = reverse_engineering_prompt(seed + i, prompt_kib)
        barrier.wait(timeout=30)
        return send(model, prompt, max_output, timeout, seed + i)

    with ThreadPoolExecutor(max_workers=count) as executor:
        futures = [executor.submit(task, i) for i in range(count)]
        barrier.wait(timeout=30)
        start = time.perf_counter()
        results = [future.result() for future in futures]
        elapsed = time.perf_counter() - start
    return {
        "elapsed_seconds": elapsed,
        "generated_tokens": sum(x["output_tokens"] for x in results),
        "prompt_tokens": sum(x["input_tokens"] or 0 for x in results),
        "requests": results,
    }


def available_memory_gib():
    try:
        row = next(line for line in Path("/proc/meminfo").read_text().splitlines()
                   if line.startswith("MemAvailable:"))
        return int(row.split()[1]) / (1024 * 1024)
    except (OSError, StopIteration, ValueError, IndexError):
        return None


def available_vram_gib():
    try:
        result = subprocess.run([
            "nvidia-smi", "--query-gpu=memory.free",
            "--format=csv,noheader,nounits",
        ], capture_output=True, text=True, timeout=10, check=True)
        return int(result.stdout.strip().splitlines()[0]) / 1024
    except (OSError, ValueError, IndexError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired):
        return None


def best_profile(rows):
    valid = [row for row in rows if row["status"] == "ok" and
             row.get("aggregate_output_tps", 0) > 0]
    if not valid:
        return None
    fastest = max(row["aggregate_output_tps"] for row in valid)
    # Within 3% of fastest, prefer fewer workers so compiler checks can run.
    near = [row for row in valid if row["aggregate_output_tps"] >= fastest * 0.97]
    return min(near, key=lambda row: (row["workers"], -row["aggregate_output_tps"]))


def save_results(rows, arguments, status: str = "partial"):
    winner = best_profile(rows)
    report = {
        "saved_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "settings": {
            "per_slot_context": arguments.per_slot_context,
            "candidates": arguments.candidates,
            "max_output": arguments.output_tokens,
            "prompt_kib": arguments.prompt_kib,
            "rounds": arguments.rounds,
        },
        "measurements": rows,
        "recommended": None,
    }
    if winner:
        report["recommended"] = {
            "workers": winner["workers"],
            "model_id": winner.get("model_id"),
            "slot_context": winner["slot_context"],
            "total_context": winner["total_context"],
            "threads": winner["threads"],
            "cpu_expert_layers": winner["cpu_expert_layers"],
            "aggregate_output_tps": winner["aggregate_output_tps"],
            "mean_latency_seconds": winner["mean_latency_seconds"],
        }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, indent=2) + "\n")
    if winner:
        PROFILE.write_text(json.dumps({
            "saved_utc": report["saved_utc"],
            "status": status,
            "recommended": report["recommended"],
            "benchmark_file": str(OUTPUT),
        }, indent=2) + "\n")
    return winner


def measure_candidate(slots: int, threads: int, cpu_expert_layers: int,
                      args, seed: int):
    context_total = slots * args.per_slot_context
    row = {
        "workers": slots,
        "threads": threads,
        "cpu_expert_layers": cpu_expert_layers,
        "total_context": context_total,
        "slot_context": 0,
        "status": "error",
    }
    proc = None
    print(f"\nTEST slots={slots}, ctx/slot={args.per_slot_context:,}, "
          f"CPU threads={threads}, CPU experts={cpu_expert_layers}", flush=True)
    try:
        # An exact context is essential: silently using less context is not a
        # fair, usable throughput comparison.
        proc, model = start_server(
            context_total, BASE, slots=slots, threads=threads,
            cpu_expert_layers=cpu_expert_layers,
            allow_context_fallback=False,
            startup_seconds=args.startup_seconds,
        )
        active_slots, ctx = query_slots(BASE)
        row["model_id"] = model["id"]
        row["slot_context"] = ctx
        if active_slots != slots or ctx < args.per_slot_context:
            raise RuntimeError(
                f"Server supplied {active_slots} slots / {ctx} tokens per slot, "
                f"expected {slots} slots / {args.per_slot_context} tokens"
            )
        memory = available_memory_gib()
        vram = available_vram_gib()
        row["available_ram_gib"] = memory
        row["available_vram_gib"] = vram
        if memory is not None and memory < args.min_ram_gib:
            raise RuntimeError(f"RAM headroom {memory:.2f} GiB below {args.min_ram_gib:.2f} GiB")
        if vram is not None and vram < args.min_vram_gib:
            raise RuntimeError(f"VRAM headroom {vram:.2f} GiB below {args.min_vram_gib:.2f} GiB")

        # Warm up the production model before timing. Never score model load.
        request_completion(model["id"],
                           reverse_engineering_prompt(seed - 1, 1),
                           64, args.timeout, seed - 1)

        rounds = [
            round_concurrency(model["id"], slots, args.prompt_kib,
                              args.output_tokens, args.timeout,
                              seed + 10000 * i)
            for i in range(args.rounds)
        ]
        generated = sum(r["generated_tokens"] for r in rounds)
        prompt = sum(r["prompt_tokens"] for r in rounds)
        duration = sum(r["elapsed_seconds"] for r in rounds)
        latencies = [x["latency_seconds"] for r in rounds for x in r["requests"]]
        row.update({
            "status": "ok",
            "generated_tokens": generated,
            "prompt_tokens": prompt,
            "measurement_seconds": duration,
            "aggregate_output_tps": round(generated / duration, 3),
            "prompt_tokens_per_wall_second": round(prompt / duration, 3),
            "per_request_output_tps": round(generated / sum(latencies), 3),
            "mean_latency_seconds": round(statistics.mean(latencies), 2),
            "max_latency_seconds": round(max(latencies), 2),
            "available_ram_gib": available_memory_gib(),
            "available_vram_gib": available_vram_gib(),
        })
        memory = row["available_ram_gib"]
        vram = row["available_vram_gib"]
        if ((memory is not None and memory < args.min_ram_gib) or
            (vram is not None and vram < args.min_vram_gib)):
            row["status"] = "memory_pressure"
            row["error"] = "Below required RAM/VRAM reserve after inference"
        print(f"  aggregate {row['aggregate_output_tps']:.2f} tok/s, "
              f"per request {row['per_request_output_tps']:.2f} tok/s, "
              f"mean latency {row['mean_latency_seconds']:.1f}s, "
              f"RAM free {memory}, VRAM free {vram} GiB, "
              f"status={row['status']}", flush=True)
    except (RuntimeError, OSError, HTTPError, URLError,
            TimeoutError, ValueError, BrokenPipeError) as exc:
        row["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
        print(f"  SKIPPED: {row['error']}", flush=True)
    finally:
        stop_server(proc)
        # Allow GPU allocations and the CPU model mapping to be released.
        time.sleep(args.release_pause)
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=lambda s: numbers(s),
                        default=[1, 2, 3, 4, 6, 8, 12, 16, 24, 32],
                        help="Parallel slot counts to benchmark, e.g. 1,2,4,6,8,12,16")
    parser.add_argument("--per-slot-context", type=int, default=32768,
                        help="Minimum usable context of each worker")
    parser.add_argument("--prompt-kib", type=int, default=8,
                        help="Approximate prompt size, KiB of technical evidence")
    parser.add_argument("--output-tokens", type=int, default=320,
                        help="Maximum generated tokens per request")
    parser.add_argument("--rounds", type=int, default=2,
                        help="Timed batches for each candidate (startup/warmup excluded)")
    parser.add_argument("--threads", type=int, default=6,
                        help="Baseline CPU threads")
    parser.add_argument("--cpu-experts", type=int, default=12,
                        help="Baseline number of CPU expert layers")
    parser.add_argument("--tune", action=argparse.BooleanOptionalAction, default=True,
                        help="After concurrency sweep, compare 4/8 threads and 10/14 CPU expert layers")
    parser.add_argument("--timeout", type=int, default=240,
                        help="Seconds per inference request")
    parser.add_argument("--startup-seconds", type=int, default=240)
    parser.add_argument("--release-pause", type=float, default=3)
    parser.add_argument("--min-ram-gib", type=float, default=2.5)
    parser.add_argument("--min-vram-gib", type=float, default=0.3)
    args = parser.parse_args(argv)
    if args.per_slot_context < 8192 or args.per_slot_context > 131072:
        parser.error("--per-slot-context must be between 8192 and 131072")
    if not 1 <= args.prompt_kib <= 64 or not 64 <= args.output_tokens <= 4096:
        parser.error("--prompt-kib 1..64, --output-tokens 64..4096")
    if not 1 <= args.rounds <= 10 or not 1 <= args.threads <= 32:
        parser.error("--rounds 1..10, --threads 1..32")
    if args.cpu_experts not in (8, 10, 12, 14, 16, 18):
        parser.error("--cpu-experts must be 8/10/12/14/16/18")
    if args.timeout < 15 or args.startup_seconds < 30 or args.release_pause < 0:
        parser.error("Invalid timeout, startup timeout or release pause")
    if query_server(BASE):
        parser.error("An inference server is already running on port 8080. "
                     "Stop llama.cpp and any active SAD workers before benchmarking.")
    if not SERVER.is_file():
        parser.error("Unsloth llama-server not installed at " + str(SERVER))
    cached_model()
    rows = []
    failures = 0
    interrupted = False
    try:
        for i, slots in enumerate(args.candidates):
            row = measure_candidate(slots, args.threads, args.cpu_experts, args,
                                    seed=100000 + i * 50000)
            rows.append(row)
            save_results(rows, args)
            if row["status"] != "ok":
                failures += 1
                if failures >= 2:
                    print("Two consecutive unfit configurations; stopping the "
                          "concurrency increase to avoid memory thrashing.", flush=True)
                    break
            else:
                failures = 0

        winner = best_profile(rows)
        if winner and args.tune:
            n = winner["workers"]
            for index, threads in enumerate((4, 8, 10, 12, 16)):
                if threads == args.threads:
                    continue
                row = measure_candidate(n, threads, args.cpu_experts, args,
                                        seed=800000 + index * 50000)
                rows.append(row)
                save_results(rows, args)
            winner = best_profile(rows)
            # Also test less/more CPU expert offload at the best CPU thread count.
            for index, experts in enumerate((10, 14)):
                if experts == args.cpu_experts:
                    continue
                row = measure_candidate(n, winner["threads"], experts, args,
                                        seed=900000 + index * 50000)
                rows.append(row)
                save_results(rows, args)
    except KeyboardInterrupt:
        interrupted = True
        print("\nBenchmark interrupted. Saving successful measurements.", flush=True)
    winner = save_results(rows, args, status="interrupted" if interrupted else "complete")
    print("\nMEASURED INKLING THROUGHPUT (generated tokens/sec, model load excluded)")
    print("slots threads CPU-experts ctx/slot  agg tok/s  tok/s/request latency  result")
    for row in rows:
        print(f"{row['workers']:>5} {row['threads']:>7} {row['cpu_expert_layers']:>11} "
              f"{row['slot_context']:>8} "
              f"{row.get('aggregate_output_tps', 0):>10.2f} "
              f"{row.get('per_request_output_tps', 0):>14.2f} "
              f"{row.get('mean_latency_seconds', 0):>7.1f}s  {row['status']}")
    if not winner:
        print("No usable benchmark configuration. Check model server logs.")
        return 1
    print(f"\nFASTEST MEASURED: {winner['workers']} workers, "
          f"{winner['aggregate_output_tps']:.2f} aggregate tokens/sec, "
          f"{winner['slot_context']} tokens/slot, "
          f"{winner['threads']} CPU threads, "
          f"{winner['cpu_expert_layers']} CPU expert layers.")
    print("This is a short throughput benchmark, not yet a 24-hour stability result.")
    print("Saved raw results: ", OUTPUT)
    print("Saved tuned profile:", PROFILE)
    print("NEXT: python3 scripts/run_local_high_effort.py --workers auto --hours 24 --max-tools 30")
    return 0


if __name__ == "__main__":
    sys.exit(main())
