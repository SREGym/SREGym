#!/usr/bin/env python3
"""Probe the configured LLM backend with a small prompt and report latency.

Run before kicking off a rerun batch to confirm the endpoint is reachable
and the latency tail looks normal.

Reads the same env vars as a real SREGym run:
    AGENT_MODEL_ID    — litellm model string
    LLM_API_BASE      — optional, override api_base
    LLM_API_KEY       — optional, override api_key
    LLM_REQUEST_TIMEOUT_S — optional, per-request timeout

Usage:
    python scripts/probe_llm_backend.py [--n 5]
"""

import argparse
import os
import sys
import time

from llm_backend.init_backend import get_llm_backend


def probe_once(backend) -> tuple[float, str]:
    t0 = time.monotonic()
    out = backend.inference(
        "Say the single word OK and nothing else.",
        system_prompt="You answer with exactly one word.",
    )
    elapsed = time.monotonic() - t0
    text = getattr(out, "content", str(out)).strip()
    return elapsed, text


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=5, help="number of probes (default 5)")
    args = ap.parse_args()

    model_id = os.environ.get("AGENT_MODEL_ID")
    if not model_id:
        print("AGENT_MODEL_ID is not set", file=sys.stderr)
        sys.exit(2)

    print(
        f"Probing model={model_id} api_base={os.environ.get('LLM_API_BASE') or '<default>'} "
        f"timeout={os.environ.get('LLM_REQUEST_TIMEOUT_S', '120')}s"
    )
    backend = get_llm_backend(model_id)

    latencies: list[float] = []
    failures = 0
    for i in range(args.n):
        try:
            elapsed, text = probe_once(backend)
            latencies.append(elapsed)
            print(f"  [{i + 1}/{args.n}] {elapsed:.2f}s  → {text[:60]!r}")
        except Exception as e:
            failures += 1
            print(f"  [{i + 1}/{args.n}] FAILED: {type(e).__name__}: {e}")

    if not latencies:
        print("\nAll probes failed.")
        sys.exit(1)

    latencies.sort()
    n = len(latencies)
    print(f"\nSummary: n={n}  failures={failures}")
    print(f"  min={latencies[0]:.2f}s  median={latencies[n // 2]:.2f}s  max={latencies[-1]:.2f}s")
    print(f"  mean={sum(latencies) / n:.2f}s")

    # Sanity gate: bail loudly if any probe took >60s — that's a bad sign for
    # a one-token completion and likely means the chronic Bedrock Converse
    # read-timeout regression is firing on this endpoint right now.
    if any(lat > 60 for lat in latencies):
        print("\n⚠️  At least one probe exceeded 60s for a one-word completion.")
        print("    Endpoint is degraded; postpone the rerun.")
        sys.exit(3)


if __name__ == "__main__":
    main()
