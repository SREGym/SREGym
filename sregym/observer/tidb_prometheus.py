import json
import os
import shlex
import subprocess

from sregym.service.telemetry.prometheus import Prometheus

FLEETCAST_NS = "fleetcast"
FLEETCAST_DEP = "fleetcast-satellite-app-backend"
FLEETCAST_METRICS_PORT = "5000"


def run_cmd(cmd, capture=False):
    if isinstance(cmd, (list, tuple)):
        cmd = [str(c) for c in cmd]
        printable = " ".join(shlex.quote(c) for c in cmd)
    else:
        printable = cmd
    print("Running:", printable)

    if capture:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT).strip()
    else:
        subprocess.run(cmd, check=True)


# def check_prometheus_targets(prom: Prometheus):
#     # Prefer env override, fallback to 9090 (your manual port-forward), then prom.port
#     port = os.environ.get("PROMETHEUS_PORT") or "9090" or str(prom.port)
#     base_url = f"http://localhost:{port}"
#     print(f"[debug] Checking Prometheus at {base_url}")

#     out = run_cmd(["curl", "-s", f"{base_url}/api/v1/targets"], capture=True)
#     try:
#         j = json.loads(out)
#         targets = j.get("data", {}).get("activeTargets", [])
#         for t in targets:
#             print("Target:", t["labels"].get("job"), t["health"])
#         print(f"[done] listed {len(targets)} targets")
#     except Exception:
#         print(out)


def ensure_fleetcast_scrape_annotations():
    """Ensure FleetCast backend is annotated for Prometheus scraping."""
    expected = {
        "prometheus.io/scrape": "true",
        "prometheus.io/path": "/metrics",
        "prometheus.io/port": FLEETCAST_METRICS_PORT,
    }

    patch = {"spec": {"template": {"metadata": {"annotations": expected}}}}
    run_cmd(
        ["kubectl", "-n", FLEETCAST_NS, "patch", "deploy", FLEETCAST_DEP, "--type=merge", "-p", json.dumps(patch)],
        capture=False,
    )

    print(f"[rollout] restarting {FLEETCAST_DEP}…")
    run_cmd(["kubectl", "-n", FLEETCAST_NS, "rollout", "restart", f"deploy/{FLEETCAST_DEP}"], capture=False)
    run_cmd(["kubectl", "-n", FLEETCAST_NS, "rollout", "status", f"deploy/{FLEETCAST_DEP}"], capture=False)

    print(f"[done] scrape annotations applied and rollout complete for {FLEETCAST_DEP}")


def main():
    prom = Prometheus()

    if not prom._is_prometheus_running():
        prom.deploy()
    else:
        print("Prometheus already running, skipping deploy.")

    # check_prometheus_targets(prom)

    ensure_fleetcast_scrape_annotations()

    # prom.stop_port_forward()


if __name__ == "__main__":
    main()
