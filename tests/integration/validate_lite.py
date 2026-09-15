"""Validate all Lite fault lifecycles serially on a disposable local KIND cluster.

No LLM is called. Each problem uses the normal Conductor deployment, workloads,
fault injector, recovery, and mitigation oracle. Stop on failure so the cluster
can be inspected before any subsequent fault runs.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from sregym.conductor.problem_sets import SREGYM_LITE_PROBLEMS
from sregym.profile import PROFILES


def cluster_identity():
    context = subprocess.check_output(["kubectl", "config", "current-context"], text=True).strip()
    if not context.startswith("kind-"):
        raise RuntimeError("Lifecycle validation is destructive; select a disposable local KIND context")
    nodes = json.loads(subprocess.check_output(["kubectl", "get", "nodes", "-o", "json"], text=True))
    cluster_name = context.removeprefix("kind-")
    local_nodes = set(subprocess.check_output(["kind", "get", "nodes", "--name", cluster_name], text=True).split())
    if not local_nodes or local_nodes != {node["metadata"]["name"] for node in nodes["items"]}:
        raise RuntimeError("The selected Kubernetes context does not match the local KIND cluster")
    for node in nodes["items"]:
        expected_provider = f"kind://docker/{cluster_name}/{node['metadata']['name']}"
        if node.get("spec", {}).get("providerID") != expected_provider:
            raise RuntimeError("The selected nodes are not from the expected Docker KIND cluster")
    return {
        "context": context,
        "nodes": sorted(
            (
                {
                    "name": node["metadata"]["name"],
                    "uid": node["metadata"]["uid"],
                    "architecture": node["status"]["nodeInfo"]["architecture"],
                }
                for node in nodes["items"]
            ),
            key=lambda node: node["name"],
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, default="full")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--resume", action="store_true", help="Skip passed problems on this same cluster/profile")
    parser.add_argument("--problems", nargs="+", choices=SREGYM_LITE_PROBLEMS, default=SREGYM_LITE_PROBLEMS)
    parser.add_argument("--problem-timeout", type=int, default=1800)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.output_dir / "suite.json"
    identity = cluster_identity()
    report = {"cluster": identity, "profile": args.profile, "with_loki": True, "attempts": []}
    if report_path.exists():
        if not args.resume:
            parser.error("Output directory already contains results; use --resume or a new directory")
        report = json.loads(report_path.read_text())
        if report["cluster"] != identity or report["profile"] != args.profile or not report["with_loki"]:
            parser.error("Existing results belong to a different cluster or profile; use a new directory")
    passed = {attempt["problem"] for attempt in report["attempts"] if attempt["passed"]}
    validator = Path(__file__).with_name("validate_problem.py")
    for problem in args.problems:
        if problem in passed:
            print(f"SKIP (already passed): {problem}", flush=True)
            continue
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        prefix = args.output_dir / f"{problem}-{stamp}"
        json_path = prefix.with_suffix(".json")
        log_path = prefix.with_suffix(".log")
        command = [
            sys.executable,
            "-u",
            str(validator),
            "--problem",
            problem,
            "--profile",
            args.profile,
            "--with-loki",
            "--summary",
            str(prefix.with_suffix(".md")),
            "--json-summary",
            str(json_path),
        ]
        print(f"START: {problem} (log: {log_path})", flush=True)
        started = time.monotonic()
        timed_out = False
        with log_path.open("w") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                process.wait(timeout=args.problem_timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            except KeyboardInterrupt:
                os.killpg(process.pid, signal.SIGTERM)
                raise
        result = json.loads(json_path.read_text()) if json_path.exists() else {}
        success = process.returncode == 0 and result.get("passed") is True and not timed_out
        report["attempts"].append(
            {
                "problem": problem,
                "passed": success,
                "timed_out": timed_out,
                "exit_code": process.returncode,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "log": str(log_path),
                "result": result,
            }
        )
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        print(f"{'PASS' if success else 'FAIL'}: {problem} ({time.monotonic() - started:.0f}s)", flush=True)
        if not success:
            print("Stopped. Inspect the log and recover cluster state before resuming.", flush=True)
            return 1
    print(f"All {len(args.problems)} selected Lite lifecycles passed. Results: {report_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
