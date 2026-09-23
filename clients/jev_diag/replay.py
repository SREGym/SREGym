"""Replay saved snapshots through the classifier without a cluster.

Loads each ``*_snapshot.json`` under a results directory, re-runs the derived
signal pass and the Jev questions, and prints the ranking per problem. With
``--expected`` (a JSON map problem_id -> component id or "other") it also scores
top-1 selection accuracy. Nothing is submitted.

    uv run python -m clients.jev_diag.replay results/0917_2216 --expected /tmp/expected.json
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

sregym_root = Path(__file__).resolve().parents[2]
if str(sregym_root) not in sys.path:
    sys.path.insert(0, str(sregym_root))

from clients.jev_diag.classifier import JevDiagnoser  # noqa: E402
from clients.jev_diag.collector import ClusterSnapshot  # noqa: E402
from clients.jev_diag.derive import post_process  # noqa: E402


def load_snapshot(path: str) -> ClusterSnapshot:
    with open(path) as handle:
        raw = json.load(handle)
    snap = ClusterSnapshot(
        app=raw.get("application") or {},
        components=raw.get("components") or {},
        cluster=raw.get("cluster") or {},
        errors=raw.get("collection_errors") or [],
    )
    post_process(snap)
    return snap


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_dir")
    parser.add_argument("--expected", help="JSON file mapping problem_id to the expected component id")
    parser.add_argument("--only", action="append", help="problem id(s) to replay")
    parser.add_argument("--out", help="write per-problem diagnoses (JSON) here")
    parser.add_argument("--no-characterize", action="store_true")
    args = parser.parse_args()
    expected = {}
    if args.expected:
        with open(args.expected) as handle:
            expected = json.load(handle)

    results = []
    hits = 0
    for snap_path in sorted(glob.glob(os.path.join(args.results_dir, "jev_diag/*/run_1/*_snapshot.json"))):
        pid = snap_path.split("/")[-3]
        if args.only and pid not in args.only:
            continue
        snap = load_snapshot(snap_path)
        diag = JevDiagnoser(characterize=not args.no_characterize).diagnose(snap)
        ranked = diag.component_result.ranked()[:3]
        exp = expected.get(pid)
        acceptable = exp if isinstance(exp, list) else ([exp] if exp else [])
        ok = diag.component in acceptable
        hits += ok
        mark = "OK " if ok else ("MISS" if exp else "    ")
        cat = diag.category_result.choice if diag.category_result else "-"
        kind = diag.fault_kind_result.choice if getattr(diag, "fault_kind_result", None) else "-"
        print(
            f"{mark} {pid:52s} pick={diag.component:38s} p={diag.component_result.probabilities.get(diag.component, 0):.2f} kind={kind} cat={cat}"
        )
        print("      top3: " + " | ".join(f"{c} {p:.2f}" for c, p in ranked) + (f"   expected={exp}" if exp else ""))
        results.append(
            {
                "problem_id": pid,
                "expected": exp,
                "pick": diag.component,
                "ranked": ranked,
                "category": cat,
                "fault_kind": kind,
                "text": diag.text,
            }
        )
    scored = [r for r in results if r["expected"]]
    if scored:
        print(f"\ntop-1 selection accuracy: {hits}/{len(scored)}")
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
