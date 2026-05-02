"""End-to-end validation harness for the new hardware-failure khaos problems.

Strategy
--------
Deploy the cluster infra (Khaos + OpenEBS + Prometheus + hotel-reservation)
exactly once via the existing Conductor.deploy_app() pipeline, bootstrapping
with the already-validated `latent_sector_error` problem. Then for each
candidate hardware problem, instantiate it fresh (cheap — no redeploy),
call inject_fault(), poll Prometheus's alerts API for firing alerts in
hotel-reservation for ~150 s, call recover_fault(), wait for symptoms to
clear, and record observations.

A problem is considered "convincing" iff it sustains at least one of the
hotel-reservation alert names defined in
`sregym/observer/prometheus/prometheus/values.yaml` while the fault is
active. Results are streamed to scripts/hardware_validation_results.jsonl.

Note on cleanup: this script intentionally does NOT trigger
conductor._cleanup_sync()'s reconcile_to_baseline step, because the
on-disk baseline predates the long-lived `sregym` namespace and would
delete the MCP server. We manually delete only the namespaces we created.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from pathlib import Path

from sregym.conductor.conductor import Conductor, ConductorConfig

CANDIDATES = [
    "nic_packet_corruption",
    "storage_controller_read_failure",
    "storage_write_failure",
    "dram_module_failure",
    "cpu_clocksource_failure",
    "mmu_page_protection_failure",
    "network_interface_link_down",
    "dns_resolver_hardware_failure",
]

OBSERVE_WINDOW_SEC = 150  # how long to watch for firing alerts after inject
RECOVERY_WAIT_SEC = 75  # pause between recover and next inject for alerts to clear
PROMETHEUS_NAMESPACE = "observe"
APP_NAMESPACE = "hotel-reservation"

OUT_PATH = Path("scripts/hardware_validation_results.jsonl")
SUMMARY_PATH = Path("scripts/hardware_validation_summary.md")


def query_firing_alerts(namespace: str = APP_NAMESPACE) -> list[dict]:
    cmd = [
        "kubectl",
        "exec",
        "-n",
        PROMETHEUS_NAMESPACE,
        "deploy/prometheus-server",
        "-c",
        "prometheus-server",
        "--",
        "wget",
        "-qO-",
        "http://localhost:9090/api/v1/alerts",
    ]
    try:
        raw = subprocess.check_output(cmd, text=True, timeout=15, stderr=subprocess.DEVNULL)
        data = json.loads(raw)
    except Exception as exc:
        print(f"[validator] alert query failed: {exc}")
        return []
    return [
        a
        for a in data.get("data", {}).get("alerts", [])
        if a.get("state") == "firing" and a.get("labels", {}).get("namespace") == namespace
    ]


def get_pod_status_summary(namespace: str = APP_NAMESPACE) -> dict[str, int]:
    """Coarse pod-state buckets so we can see if pods are restart-looping etc."""
    try:
        raw = subprocess.check_output(
            ["kubectl", "-n", namespace, "get", "pods", "-o", "json"],
            text=True,
            timeout=15,
            stderr=subprocess.DEVNULL,
        )
        data = json.loads(raw)
    except Exception:
        return {}
    counts: dict[str, int] = {}
    for p in data.get("items", []):
        phase = p.get("status", {}).get("phase", "?")
        ready = sum(1 for c in p.get("status", {}).get("containerStatuses", []) or [] if c.get("ready"))
        total = len(p.get("status", {}).get("containerStatuses", []) or [])
        restarts = sum(int(c.get("restartCount", 0)) for c in p.get("status", {}).get("containerStatuses", []) or [])
        bucket = f"{phase}({ready}/{total})"
        counts[bucket] = counts.get(bucket, 0) + 1
        if restarts:
            counts["__restarts__"] = counts.get("__restarts__", 0) + restarts
    return counts


def fmt_alert(a: dict) -> str:
    labels = a.get("labels", {})
    return (
        f"{labels.get('alertname', '?')} ({labels.get('service_name') or labels.get('pod', '')})"
        f" [{labels.get('severity', '')}]"
    )


def wait_for_alerts_to_clear(seconds: int) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        alerts = query_firing_alerts()
        if not alerts:
            return
        time.sleep(10)


def append_jsonl(record: dict) -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")


def write_summary(records: list[dict]) -> None:
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Hardware-problem validation summary",
        "",
        f"_Run finished at_ `{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}`",
        "",
        "| Problem ID | Bit app? | Alert names | Pod buckets at peak | Notes |",
        "|---|---|---|---|---|",
    ]
    for r in records:
        bit = "✅" if r.get("bit_app") else "❌"
        alerts = ", ".join(r.get("alert_names", [])) or "—"
        pods = ", ".join(f"{k}:{v}" for k, v in (r.get("peak_pod_state", {}) or {}).items()) or "—"
        notes = r.get("error", "") or ""
        lines.append(f"| {r['problem_id']} | {bit} | {alerts} | {pods} | {notes} |")
    SUMMARY_PATH.write_text("\n".join(lines) + "\n")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text("")

    print("=== Bootstrap: deploy infra via conductor ===", flush=True)
    config = ConductorConfig(deploy_loki=False, enable_noise=False)
    conductor = Conductor(config=config)

    bootstrap_id = "latent_sector_error"
    conductor.problem_id = bootstrap_id
    conductor.problem = conductor.problems.get_problem_instance(bootstrap_id)
    conductor.app = conductor.problem.app

    # Deploy everything: khaos, openebs, prometheus, hotel-reservation, workload.
    # This is the heavy step (~5-10 min). Subsequent problems just reuse the cluster.
    conductor.deploy_app()
    print("=== Infra ready, starting per-problem validation ===", flush=True)

    records: list[dict] = []
    for pid in CANDIDATES:
        print(f"\n\n========== Validating: {pid} ==========", flush=True)
        record: dict = {"problem_id": pid, "alert_names": [], "alerts": [], "bit_app": False}
        problem = None
        try:
            problem = conductor.problems.get_problem_instance(pid)
            print(f"[{pid}] injecting fault...", flush=True)
            problem.inject_fault()

            seen_alerts: dict[str, dict] = {}
            peak_pods: dict[str, int] = {}
            t0 = time.monotonic()
            while time.monotonic() - t0 < OBSERVE_WINDOW_SEC:
                alerts = query_firing_alerts()
                for a in alerts:
                    name = a.get("labels", {}).get("alertname", "?")
                    target = a.get("labels", {}).get("service_name") or a.get("labels", {}).get("pod", "")
                    seen_alerts[f"{name}/{target}"] = a
                pods = get_pod_status_summary()
                if pods:
                    # Track the pod-state distribution at peak observed disruption.
                    if not peak_pods or sum(pods.values()) >= sum(peak_pods.values()):
                        peak_pods = pods
                if alerts:
                    elapsed = int(time.monotonic() - t0)
                    summary = ", ".join(fmt_alert(a) for a in alerts[:5])
                    print(f"[{pid}] t={elapsed}s firing: {summary}", flush=True)
                time.sleep(10)

            record["alerts"] = list(seen_alerts.values())
            record["alert_names"] = sorted({a["labels"].get("alertname", "?") for a in seen_alerts.values()})
            record["bit_app"] = bool(seen_alerts)
            record["peak_pod_state"] = peak_pods
            print(f"[{pid}] alerts={record['alert_names']} bit={record['bit_app']}", flush=True)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            print(f"[{pid}] ERROR: {err}", flush=True)
            record["error"] = err
        finally:
            if problem is not None:
                try:
                    print(f"[{pid}] recovering fault...", flush=True)
                    problem.recover_fault()
                except Exception as exc:
                    print(f"[{pid}] WARN recover failure: {exc}", flush=True)

        # Wait for alerts to clear before next iteration so we don't carry-over signal.
        print(f"[{pid}] waiting up to {RECOVERY_WAIT_SEC}s for alerts to clear...", flush=True)
        wait_for_alerts_to_clear(RECOVERY_WAIT_SEC)
        leftover = query_firing_alerts()
        if leftover:
            print(f"[{pid}] WARN leftover alerts after recovery: {[fmt_alert(a) for a in leftover[:5]]}", flush=True)
            record["leftover_after_recovery"] = sorted({a["labels"].get("alertname", "?") for a in leftover})

        records.append(record)
        append_jsonl(record)
        write_summary(records)

    print("\n\n=== Per-problem validation complete ===", flush=True)

    # Manual cleanup. We avoid conductor._cleanup_sync() because its
    # reconcile_to_baseline step would delete the long-lived `sregym` namespace
    # (the MCP server's home) along with the deployed infra.
    print("=== Cleaning up deployed namespaces ===", flush=True)
    for ns in ["hotel-reservation", "observe", "khaos", "openebs"]:
        subprocess.run(
            ["kubectl", "delete", "ns", ns, "--ignore-not-found", "--timeout=120s"],
            check=False,
        )

    print("\n=== Summary ===", flush=True)
    for r in records:
        bit = "BIT" if r.get("bit_app") else "no-bite"
        print(f"  {r['problem_id']:40s} {bit:10s} alerts={r.get('alert_names', [])}", flush=True)
    print(f"\nSummary written to: {SUMMARY_PATH}", flush=True)
    print(f"JSONL records:      {OUT_PATH}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
