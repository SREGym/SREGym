"""No-fault Astronomy baseline: python -m scripts.baseline.run --help."""

import argparse
import contextlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from sregym.observer.baseline import collect, run, save


def deploy():
    # Imported only by the deployment child. Never use start_problem(), which
    # injects a fault and may invoke an agent/oracle.
    from sregym.conductor.conductor import Conductor
    from sregym.conductor.problems.base import Problem
    from sregym.profile import set_profile
    from sregym.service.apps.astronomy_shop import AstronomyShop

    class Baseline(Problem):
        def __init__(self):
            super().__init__(app=AstronomyShop())
            self.app.create_workload()

        def inject_fault(self):
            raise RuntimeError("Fault injection is forbidden in a baseline")

        def recover_fault(self):
            raise RuntimeError("No baseline fault exists")

    set_profile(os.environ["SREGYM_PROFILE"])
    conductor = Conductor()
    conductor.problem = Baseline()
    conductor.problem_id = "astronomy_shop_no_fault_baseline"
    conductor.deploy_app()


def traffic_errors(sample, max_fail_ratio):
    try:
        response = sample["http_probe"]
        if response["rc"] != 0:
            return ["http_probe_failed"]
        probe = json.loads(response["out"])
        errors = []
        products, workload = probe["products"], probe["workload"]
        if products.get("status") != 200 or not products.get("product_count"):
            errors.append("products_unhealthy")
        if workload.get("state") != "running" or not workload.get("total_rps", 0):
            errors.append("traffic_not_running")
        ratio = workload.get("fail_ratio")
        if ratio is None or ratio > max_fail_ratio:
            errors.append("traffic_failure_ratio")
        return errors
    except (KeyError, ValueError, TypeError):
        return ["http_probe_invalid"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True, help="Dedicated kind cluster context (must also be current)")
    parser.add_argument("--output", required=True, type=Path, help="New evidence directory; never overwritten")
    parser.add_argument(
        "--seconds", type=int, default=3600, help="Observation time after deployment, excluding startup"
    )
    parser.add_argument("--interval", type=int, default=20)
    parser.add_argument("--startup-timeout", type=int, default=1800)
    parser.add_argument("--profile", choices=["full", "svelte"], default="full")
    parser.add_argument("--max-fail-ratio", type=float, default=0.01)
    parser.add_argument("--observe-existing", action="store_true", help="Profile an experiment without deploying")
    args = parser.parse_args()
    if min(args.seconds, args.interval, args.startup_timeout) <= 0 or not 0 <= args.max_fail_ratio <= 1:
        parser.error("Durations must be positive and failure ratio must be between zero and one")
    context = run(["kubectl", "config", "current-context"])
    if context.get("out", "").strip() != args.context or not args.context.startswith("kind-"):
        parser.error("Select the dedicated kind context as the current context first")
    cluster = args.context.removeprefix("kind-")
    nodes = run(["kind", "get", "nodes", "--name", cluster])
    if nodes["rc"] or not nodes.get("out", "").strip():
        parser.error("Dedicated kind cluster is unavailable")
    nodes = nodes["out"].split()
    existing = run(
        ["kubectl", "--context", args.context, "get", "namespace", "astronomy-shop", "--ignore-not-found", "-o", "name"]
    )
    if existing["rc"] or (existing["out"].strip() and not args.observe_existing):
        parser.error("Fresh baseline requires no astronomy-shop namespace; preserve evidence and clean up first")
    if not args.observe_existing and shutil.disk_usage(".").free < 25 * 1024**3:
        parser.error("Fresh deployment requires at least 25 GiB of free disk")
    args.output.mkdir(parents=True, exist_ok=False)
    os.environ["SREGYM_PROFILE"] = args.profile
    manifest = {
        "architecture": platform.machine(),
        "kernel": platform.release(),
        "python": sys.version,
        "parameters": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
        "versions": {
            name: run(command)
            for name, command in {
                "git": ["git", "rev-parse", "HEAD"],
                "diff": ["git", "diff", "--no-ext-diff"],
                "submodules": ["git", "submodule", "status", "--recursive"],
                "docker": ["docker", "version"],
                "kind": ["kind", "version"],
                "kubectl": ["kubectl", "version", "-o", "json"],
                "helm": ["helm", "version"],
            }.items()
        },
    }
    save(args.output / "environment.json", manifest)
    process = None
    status, errors = "interrupted", []
    start = time.monotonic()
    observed_start = start if args.observe_existing else None
    count = 0
    initial = collect(args.context, nodes)
    save(args.output / "initial.json", initial)
    initial_restarts = {
        (f["uid"], f["container"]): f["restarts"]
        for f in initial.get("findings", [])
        if f["reason"] == "container_restarted"
    }
    # Startup restarts/OOMs must be retained even if a pod is later replaced.
    oom_evidence = {}
    for item in initial.get("findings", []):
        if item["reason"] == "OOMKilled":
            key = json.dumps([item["uid"], item["container"], item["termination"]], sort_keys=True)
            oom_evidence[key] = item
    original_signal = signal.getsignal(signal.SIGTERM)

    def terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    try:
        with (args.output / "deployment.log").open("w") as deployment_log:
            if not args.observe_existing:
                cache = Path.home() / "cache_dir/cluster_baseline_state.json"
                if cache.exists():
                    shutil.copy2(cache, args.output / "prior-cluster-baseline.json")
                    cache.unlink()
                process = subprocess.Popen(
                    [sys.executable, "-m", "scripts.baseline.run", "--deploy"],
                    stdout=deployment_log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            while True:
                now = time.monotonic()
                if process and observed_start is None:
                    code = process.poll()
                    if code is not None:
                        if code:
                            status, errors = "setup_error", [f"deployment_exit_{code}"]
                            break
                        observed_start = now
                    elif now - start > args.startup_timeout:
                        status, errors = "setup_error", ["deployment_timeout"]
                        break
                sample = collect(args.context, nodes, traffic=observed_start is not None)
                sample["phase"] = "observing" if observed_start is not None else "deploying"
                sample["elapsed_seconds"] = time.monotonic() - start
                sample["disk_free_bytes"] = shutil.disk_usage(args.output).free
                for item in sample.get("findings", []):
                    if item["reason"] == "OOMKilled":
                        key = json.dumps([item["uid"], item["container"], item["termination"]], sort_keys=True)
                        oom_evidence[key] = item
                for item in sample.get("findings", []):
                    if item["reason"] == "container_restarted" and item["restarts"] > initial_restarts.get(
                        (item["uid"], item["container"]), 0
                    ):
                        errors.append("container_restarted")
                for name, result in sample.items():
                    if name.startswith("cgroup/"):
                        for cid, fields in result.get("containers", {}).items():
                            events = dict(line.split() for line in fields.get("memory.events", "").splitlines())
                            if int(events.get("oom_kill", 0)):
                                oom_evidence[cid] = {"container_id": cid, "reason": "cgroup_oom_kill", "events": events}
                save(args.output / f"sample-{count:05d}.json", sample)
                count += 1
                print(
                    json.dumps(
                        {
                            "sample": count,
                            "phase": sample["phase"],
                            "elapsed": int(sample["elapsed_seconds"]),
                            "oom_signals": len(oom_evidence),
                        }
                    ),
                    flush=True,
                )
                if sample["disk_free_bytes"] < 3 * 1024**3:
                    status, errors = "capacity_stop", ["disk_below_3GiB"]
                    break
                if oom_evidence:
                    status, errors = "baseline_oom", ["OOMKilled"]
                    break
                if observed_start is not None:
                    errors += traffic_errors(sample, args.max_fail_ratio)
                    errors += [f["reason"] for f in sample.get("findings", []) if f["reason"] != "container_restarted"]
                    errors += [
                        name + "_unavailable"
                        for name, result in sample.items()
                        if isinstance(result, dict) and result.get("rc", 0) != 0
                    ]
                    if "collection_error" in sample:
                        errors.append("collection_error")
                    if time.monotonic() - observed_start >= args.seconds:
                        status = "health_check_failed" if errors else "health_check_passed"
                        break
                time.sleep(args.interval)
    except KeyboardInterrupt:
        status = "interrupted"
    except Exception as exc:
        status, errors = "collection_error", [str(exc)]
    finally:
        signal.signal(signal.SIGTERM, original_signal)
        if process:
            # Also stop deployment-owned port forwards after the child exits.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        result = {
            "status": status,
            "errors": sorted(set(errors)),
            "samples": count,
            "oom_evidence": list(oom_evidence.values()),
            "elapsed_seconds": time.monotonic() - start,
            "observation_seconds": time.monotonic() - observed_start if observed_start else 0,
            "fault_injected": False,
            "agent_invoked": False,
            "qualified": False,
            "note": "Health checks alone do not qualify memory stability. Review cgroup curves, "
            "three fresh sustained runs, cleanup/redeployment, and native x86 evidence.",
        }
        save(args.output / "result.json", result)
        save(args.output / "final.json", collect(args.context, nodes, traffic=observed_start is not None))
        save(args.output / "helm-releases.json", run(["helm", "list", "-A", "-o", "json"]))
        save(args.output / "kernel.json", run(["sudo", "-n", "dmesg"]))
        save(
            args.output / "kind-export.json",
            run(["kind", "export", "logs", str(args.output / "kind-logs"), "--name", cluster], timeout=120),
        )
        print(json.dumps(result), flush=True)
    return 0 if status == "health_check_passed" else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--deploy"]:
        deploy()
    else:
        sys.exit(main())
