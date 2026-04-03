"""
Mock Agent Data Generator
==========================
Generates realistic synthetic results CSVs for several fictional agents
so the leaderboard looks like a real competition.

Pulls the actual problem IDs from your existing runs so the data is grounded
in the real benchmark — just with simulated pass/fail outcomes.

Usage:
    python leaderboard/generate_mock_agents.py          # write to submissions/
    python leaderboard/generate_mock_agents.py --dry-run # just print, don't write
"""

import argparse
import csv
import glob
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent

# ── Pull real problem IDs from existing CSVs ────────────────────────────────
def get_known_problems() -> list[str]:
    problems = set()
    for path in ROOT.glob("*.csv"):
        with open(path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                pid = (row.get("problem_id") or "").strip()
                if pid and pid not in ("problem_id", "attempt"):
                    problems.add(pid)
    return sorted(problems)

# ── Category helpers (mirrors leaderboard.py) ───────────────────────────────
def get_category(pid: str) -> str:
    if pid.startswith("operator_"):           return "K8s Operator"
    if pid in {"silent_data_corruption", "latent_sector_error", "read_error"}:
        return "Hardware Faults"
    if pid.startswith("trainticket_"):        return "Train Ticket"
    if pid.startswith("astronomy_shop_") or pid in {"kafka_queue_problems", "loadgenerator_flood_homepage"}:
        return "OpenTelemetry"
    if pid in {"capacity_decrease_rpc_retry_storm", "gc_capacity_degradation", "load_spike_rpc_retry_storm"}:
        return "Metastable"
    if pid.endswith("_correlated"):           return "Correlated Faults"
    if any(pid.startswith(p) for p in ("incorrect_","missing_env_variable","revoke_auth_","storage_user_","valkey_","misconfig_app","auth_miss_mongodb")):
        return "Application Faults"
    return "K8s Config"

# ── Agent profiles ────────────────────────────────────────────────────────────
# Each profile defines base pass rates per category and overall variance.
# These are fictional — meant to show differentiated strengths/weaknesses.
AGENT_PROFILES = {
    "resolve": {
        "description": "Resolve AI — strong on config errors, weak on metastable",
        "base_diag": 0.72,
        "base_mitig": 0.55,
        "category_boost": {
            "K8s Config":        +0.18,
            "Application Faults": +0.10,
            "K8s Operator":      -0.05,
            "Metastable":        -0.25,
            "Hardware Faults":   -0.20,
            "OpenTelemetry":     +0.05,
        },
        "mitig_given_diag": 0.68,
        "avg_ttl_s": 145,
        "avg_ttm_s": 310,
        "ttl_jitter": 60,
        "ttm_jitter": 120,
    },
    "ciroos": {
        "description": "Ciroos — fast diagnosis, lower mitigation rate",
        "base_diag": 0.65,
        "base_mitig": 0.45,
        "category_boost": {
            "K8s Config":        +0.05,
            "Application Faults": +0.15,
            "K8s Operator":      +0.08,
            "Metastable":        -0.15,
            "Hardware Faults":   -0.30,
            "OpenTelemetry":     +0.10,
        },
        "mitig_given_diag": 0.58,
        "avg_ttl_s": 98,
        "avg_ttm_s": 210,
        "ttl_jitter": 40,
        "ttm_jitter": 90,
    },
    "claude-agent": {
        "description": "Claude-based agent — balanced across categories",
        "base_diag": 0.78,
        "base_mitig": 0.62,
        "category_boost": {
            "K8s Config":        +0.10,
            "Application Faults": +0.08,
            "K8s Operator":      +0.05,
            "Metastable":        -0.10,
            "Hardware Faults":   -0.15,
            "OpenTelemetry":     +0.12,
            "Train Ticket":      +0.05,
        },
        "mitig_given_diag": 0.74,
        "avg_ttl_s": 178,
        "avg_ttm_s": 345,
        "ttl_jitter": 80,
        "ttm_jitter": 150,
    },
    "k8s-gpt": {
        "description": "K8s-GPT — very good at K8s native problems, poor on app-level",
        "base_diag": 0.60,
        "base_mitig": 0.48,
        "category_boost": {
            "K8s Config":        +0.30,
            "K8s Operator":      +0.25,
            "Application Faults": -0.20,
            "OpenTelemetry":     -0.25,
            "Metastable":        -0.30,
            "Train Ticket":      -0.35,
            "Hardware Faults":   -0.10,
        },
        "mitig_given_diag": 0.72,
        "avg_ttl_s": 112,
        "avg_ttm_s": 225,
        "ttl_jitter": 45,
        "ttm_jitter": 80,
    },
}


def simulate_result(problem_id: str, profile: dict, rng: random.Random) -> dict:
    cat = get_category(problem_id)
    boost = profile["category_boost"].get(cat, 0.0)
    diag_prob = max(0.0, min(1.0, profile["base_diag"] + boost))
    diag = rng.random() < diag_prob

    if diag:
        mitig_prob = max(0.0, min(1.0, profile["mitig_given_diag"] + boost * 0.5))
        mitig = rng.random() < mitig_prob
    else:
        mitig = False

    ttl = max(20.0, rng.gauss(profile["avg_ttl_s"], profile["ttl_jitter"]))
    ttm = max(ttl + 10, rng.gauss(profile["avg_ttm_s"], profile["ttm_jitter"])) if mitig else None

    return {
        "problem_id": problem_id,
        "Diagnosis.success": str(diag),
        "Diagnosis.accuracy": "100.0" if diag else "0.0",
        "Diagnosis.judgment": str(diag),
        "Mitigation.success": str(mitig),
        "TTL": f"{ttl:.3f}",
        "TTM": f"{ttm:.3f}" if ttm is not None else "",
        "attempt": "1",
    }


def write_agent_csv(agent_name: str, rows: list[dict], out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "results.csv"
    fieldnames = ["problem_id", "Diagnosis.success", "Diagnosis.accuracy",
                  "Diagnosis.judgment", "Mitigation.success", "TTL", "TTM", "attempt"]
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Generate mock agent submissions for leaderboard testing")
    parser.add_argument("--dry-run", action="store_true", help="Print summary without writing files")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    parser.add_argument("--agents", nargs="+", default=list(AGENT_PROFILES.keys()),
                        help="Which agents to generate (default: all)")
    args = parser.parse_args()

    problems = get_known_problems()
    if not problems:
        print("No problem IDs found — make sure *_ALL_results.csv files exist in the root.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(problems)} problems from existing runs:")
    for p in problems:
        print(f"  {p}")
    print()

    submissions_dir = ROOT / "leaderboard" / "submissions"

    for agent_name in args.agents:
        if agent_name not in AGENT_PROFILES:
            print(f"Unknown agent: {agent_name}. Available: {list(AGENT_PROFILES)}")
            continue

        profile = AGENT_PROFILES[agent_name]
        rng = random.Random(args.seed + hash(agent_name) % 9999)

        rows = [simulate_result(pid, profile, rng) for pid in problems]
        diag_pass = sum(1 for r in rows if r["Diagnosis.success"] == "True")
        mitig_pass = sum(1 for r in rows if r["Mitigation.success"] == "True")

        if args.dry_run:
            print(f"[DRY RUN] {agent_name}:")
            print(f"  {profile['description']}")
            print(f"  Diagnosis: {diag_pass}/{len(rows)} = {diag_pass/len(rows)*100:.1f}%")
            print(f"  Mitigation: {mitig_pass}/{len(rows)} = {mitig_pass/len(rows)*100:.1f}%")
            print()
        else:
            out_dir = submissions_dir / agent_name
            out_path = write_agent_csv(agent_name, rows, out_dir)
            print(f"✓ {agent_name}: {diag_pass}/{len(rows)} diag ({diag_pass/len(rows)*100:.1f}%), "
                  f"{mitig_pass}/{len(rows)} mitig ({mitig_pass/len(rows)*100:.1f}%) → {out_path}")

    if not args.dry_run:
        print()
        print("Regenerate leaderboard:")
        print("  python3 leaderboard/leaderboard.py")
        print()
        print("Preview as one of these agents before submitting:")
        print("  python3 leaderboard/leaderboard.py preview --agent ciroos --csv leaderboard/submissions/ciroos/results.csv")


if __name__ == "__main__":
    main()
