"""Replay saved runs without a cluster.

Two modes:

* Saved snapshots (default): loads each ``*_snapshot.json`` under a results directory, re-runs the derived
  signal pass and the one-shot Jev questions, and prints the ranking per problem. With ``--expected`` (a JSON
  map problem_id -> component id or "other") it also scores top-1 selection accuracy. Nothing is submitted.

      uv run python -m clients.jev_diag.replay results/0917_2216 --expected /tmp/expected.json

* Raw bundles (``--raw``): serves kubectl from a ``*_kubectl_raw.jsonl.gz`` recording, with the clock pinned
  to the recording, and re-runs the whole collection offline; ``--diagnose`` then runs the decision tree
  (Jev calls; per-component detail reads are served from the bundle when the original run made them).

      uv run python -m clients.jev_diag.replay --raw logs/jev_diag_x_20260924_101010_kubectl_raw.jsonl.gz
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
from clients.jev_diag.collector import ClusterSnapshot, collect_snapshot, set_kubectl_replay  # noqa: E402
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
    if raw.get("collected_at"):
        snap.collected_at = raw["collected_at"]
    post_process(snap)
    return snap


def replay_raw(bundle: str, diagnose: bool) -> None:
    trace_path = bundle.replace("_kubectl_raw.jsonl.gz", "_trace.jsonl")
    app_info = None
    if os.path.exists(trace_path):
        with open(trace_path) as handle:
            for line in handle:
                rec = json.loads(line)
                if rec.get("phase") == "setup" and rec.get("event") == "app_info":
                    app_info = rec.get("app_info")
                    break
    if not app_info:
        sys.exit(f"no app_info found in {trace_path}")
    set_kubectl_replay(bundle)
    try:
        snapshot = collect_snapshot(app_info, alerts_fetcher=None)
        print(
            f"symptom onset: {snapshot.cluster.get('symptom_onset')} ({snapshot.cluster.get('symptom_onset_source')})"
        )
        for change in snapshot.cluster.get("recent_changes") or []:
            print(f"change: {change}")
        for cid, comp in snapshot.components.items():
            if comp.get("signals"):
                print(f"{cid} {comp.get('evidence_kinds')}")
                for sig in comp["signals"]:
                    print(f"    - {sig[:220]}")
        if diagnose:
            from clients.jev_diag.investigate import collect_component_detail
            from clients.jev_diag.tree import IterativeDiagnoser

            diagnosis = IterativeDiagnoser(fetch_detail=collect_component_detail).diagnose(snapshot)
            print(diagnosis.text)
    finally:
        set_kubectl_replay(None)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_dir", nargs="?")
    parser.add_argument("--expected", help="JSON file mapping problem_id to the expected component id")
    parser.add_argument("--only", action="append", help="problem id(s) to replay")
    parser.add_argument("--out", help="write per-problem diagnoses (JSON) here")
    parser.add_argument("--no-characterize", action="store_true")
    parser.add_argument("--raw", help="replay collection from a *_kubectl_raw.jsonl.gz bundle")
    parser.add_argument("--diagnose", action="store_true", help="with --raw: run the decision tree (calls Jev)")
    args = parser.parse_args()
    if args.raw:
        replay_raw(args.raw, args.diagnose)
        return
    if not args.results_dir:
        parser.error("results_dir is required without --raw")
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
