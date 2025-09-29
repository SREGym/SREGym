import subprocess
import shlex
import json
import time
import re
from pathlib import Path
from typing import Optional, List, Dict

PROM_NAMESPACE = "observe"
PROM_RELEASE   = "prometheus"
PROM_CHART     = "prometheus-community/prometheus"
PROM_SVC_NAME  = "prometheus-server"

PROM_PORT         = 80         
DESIRED_NODEPORT  = 32000         

FLEETCAST_NS           = "fleetcast"
FLEETCAST_DEP          = "fleetcast-satellite-app-backend"
FLEETCAST_METRICS_PORT = "5000"

PROM_VALUES_PATH = (Path(__file__).parent / "../../../aiopslab-applications/FleetCast/prometheus/prometheus.yaml").resolve()

RETRY_SECS    = 60
SLEEP_BETWEEN = 3


CURL_CONNECT_TIMEOUT = "3"   
CURL_MAX_TIME        = "8"   

def run_cmd(cmd, shell: bool = False, check: bool = True, capture: bool = False, timeout: int | None = None) -> str:
    """
    Run a command. Accepts list/tuple (preferred) or str (when shell=True).
    Coerces any Path objects to str and prints a shell-quoted version for clarity.
    Supports a timeout (seconds).
    """
    if isinstance(cmd, (list, tuple)):
        cmd = [str(c) for c in cmd]
        printable = " ".join(shlex.quote(c) for c in cmd)
    else:
        printable = cmd
    print("Running:", printable)

    if capture:
        if shell:
            out = subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.STDOUT, timeout=timeout)
        else:
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT, timeout=timeout)
        return out.strip()
    else:
        subprocess.run(cmd, shell=shell, check=check, timeout=timeout)
        return ""


def ns_exists(ns: str) -> bool:
    return subprocess.run(
        ["kubectl", "get", "ns", ns],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode == 0


def ensure_ns(ns: str):
    if not ns_exists(ns):
        run_cmd(["kubectl", "create", "ns", ns])


def helm_repo_setup():
    try:
        run_cmd(["helm", "repo", "add", "prometheus-community",
                 "https://prometheus-community.github.io/helm-charts"])
    except subprocess.CalledProcessError:
        pass
    run_cmd(["helm", "repo", "update"])


def helm_apply_values():
    """
    (Re)install/upgrade the Helm chart using your values file.
    We also explicitly set the Service to NodePort:32000 for reachability.
    (This was the old way — now we exec inside the pod instead of NodePort.)
    """
    run_cmd([
        "helm", "upgrade", PROM_RELEASE, PROM_CHART,
        "-n", PROM_NAMESPACE, "--install", "--reset-values",
        "-f", str(PROM_VALUES_PATH), 
        "--set", "kubeStateMetrics.enabled=false",
    ])


def _get_json(args):
    out = run_cmd(args + ["-o", "json"], capture=True, check=False)
    return json.loads(out) if out else {}


def _first(rs_list):
    return sorted(rs_list, key=lambda x: x["metadata"]["creationTimestamp"], reverse=True)[0] if rs_list else None


def dump_prometheus_debug():
    try:
        print("\n=== describe deploy/prometheus-server ===")
        run_cmd(["kubectl","-n",PROM_NAMESPACE,"describe","deploy","prometheus-server"], check=False)
    except Exception:
        pass
    try:
        print("\n=== replicasets ===")
        run_cmd(["kubectl","-n",PROM_NAMESPACE,"get","rs","-o","wide"], check=False)
    except Exception:
        pass
    try:
        print("\n=== pods (all) ===")
        run_cmd(["kubectl","-n",PROM_NAMESPACE,"get","po","-o","wide"], check=False)
    except Exception:
        pass
    try:
        print("\n=== recent events ===")
        run_cmd(["kubectl","-n",PROM_NAMESPACE,"get","events","--sort-by=.lastTimestamp"], check=False)
    except Exception:
        pass


def check_configmap_for_duplicate_global() -> None:
    """
    Fetch the rendered prometheus.yml and warn if multiple 'global:' blocks exist.
    That condition crashes Prometheus on startup.
    """
    try:
        raw = run_cmd(
            ["kubectl", "-n", PROM_NAMESPACE, "get", "cm", "prometheus-server",
             "-o", r"jsonpath={.data.prometheus\.yml}"],
            capture=True, check=False
        )
    except Exception:
        raw = ""
    if not raw:
        return
    count = len(re.findall(r"(?m)^\s*global:\s*$", raw))
    if count > 1:
        print("[warn] Detected multiple 'global:' blocks in rendered prometheus.yml.")
        print("       Remove duplicates (keep exactly one global: OR none) in your values file.")


def wait_for_prometheus_ready(timeout_seconds=420):
    deadline = time.time() + timeout_seconds
    printed_stuck = False

    while time.time() < deadline:
        dep = _get_json(["kubectl","-n",PROM_NAMESPACE,"get","deploy",PROM_SVC_NAME])
        if not dep:
            print("[info] Deployment not found yet…")
            time.sleep(2); continue

        for c in dep.get("status", {}).get("conditions", []):
            if c.get("type")=="Progressing" and c.get("reason")=="ProgressDeadlineExceeded":
                if not printed_stuck:
                    print(f"[error] Deployment stuck: {c.get('reason')} — {c.get('message')}")
                    dump_prometheus_debug()
                    printed_stuck = True
                return

        rs_all = _get_json(["kubectl","-n",PROM_NAMESPACE,"get","rs"]).get("items", [])
        owned = [rs for rs in rs_all if any(
            o.get("kind")=="Deployment" and o.get("name")==PROM_SVC_NAME
            for o in rs.get("metadata",{}).get("ownerReferences",[])
        )]
        rs = _first(owned)
        if not rs:
            print("[info] No ReplicaSet yet, waiting…")
            time.sleep(2); continue

        pth = rs.get("metadata",{}).get("labels",{}).get("pod-template-hash")
        if not pth:
            print("[info] RS missing pod-template-hash, waiting…")
            time.sleep(2); continue

        pods = _get_json(["kubectl","-n",PROM_NAMESPACE,"get","pods","-l",f"pod-template-hash={pth}"]).get("items", [])
        if not pods:
            print("[info] No pods for current RS, waiting…")
            time.sleep(2); continue

        for p in pods:
            conds = p.get("status",{}).get("conditions",[])
            if any(c.get("type")=="Ready" and c.get("status")=="True" for c in conds):
                print("Prometheus server pod is Ready.")
                return

        states = ", ".join(f'{pp["metadata"]["name"]}:{pp.get("status",{}).get("phase","")}' for pp in pods)
        print("Waiting on pods:", states)
        time.sleep(2)

    print("\n[warn] Timed out waiting for Prometheus Ready. Diagnostics:")
    dump_prometheus_debug()


def check():
    out = run_cmd([
        "kubectl", "-n", PROM_NAMESPACE,
        "exec", f"deploy/{PROM_SVC_NAME}", "-c", "prometheus-server", "--",
        "wget", "-qO-", "http://localhost:9090/-/ready"
    ], capture=True)
    print("Prometheus readiness:", out, flush=True)

    out = run_cmd([
        "kubectl", "-n", PROM_NAMESPACE,
        "exec", f"deploy/{PROM_SVC_NAME}", "-c", "prometheus-server", "--",
        "wget", "-qO-", "http://localhost:9090/api/v1/targets"
    ], capture=True)
    try:
        j = json.loads(out)
        for t in j.get("data", {}).get("activeTargets", []):
            print("Target:", t["labels"].get("job"), t["health"], flush=True)
    except Exception:
        print(out, flush=True)

def ensure_fleetcast_scrape_annotations():
    """
    Ensure the FleetCast backend Deployment has the Prometheus scrape annotations.
    If missing, patch it and rollout restart.
    """
    dep_name = FLEETCAST_DEP
    ns = FLEETCAST_NS

    print(f"[debug] checking scrape annotations on {ns}/{dep_name}")

    # Fetch deployment spec
    dep = _get_json(["kubectl", "-n", ns, "get", "deploy", dep_name])
    tmpl = dep.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations", {})

    expected = {
        "prometheus.io/scrape": "true",
        "prometheus.io/path": "/metrics",
        "prometheus.io/port": FLEETCAST_METRICS_PORT,
    }

    missing = {k: v for k, v in expected.items() if tmpl.get(k) != v}

    if not missing:
        print(f"[ok] scrape annotations already present on {dep_name}")
        return

    print(f"[patch] adding scrape annotations: {missing}")
    patch = {"spec": {"template": {"metadata": {"annotations": expected}}}}
    run_cmd([
        "kubectl", "-n", ns, "patch", "deploy", dep_name,
        "--type=merge", "-p", json.dumps(patch)
    ], check=False)

    print(f"[rollout] restarting {dep_name}…")
    run_cmd(["kubectl", "-n", ns, "rollout", "restart", f"deploy/{dep_name}"], check=False)
    run_cmd(["kubectl", "-n", ns, "rollout", "status", f"deploy/{dep_name}"], check=False)

    print(f"[done] scrape annotations applied and rollout complete for {dep_name}")



# def annotate_fleetcast_and_rollout():
#     """
#     Ensure the fleetcast Deployment's *pod template* carries Prometheus annotations,
#     then restart it so pods are recreated with those annotations.
#     """
#     ns = FLEETCAST_NS if ns_exists(FLEETCAST_NS) else find_dep_namespace(FLEETCAST_DEP)
#     if not ns:
#         print(f"(skip) Could not find deployment '{FLEETCAST_DEP}' to annotate.")
#         return
#
#     print(f"Patching pod template annotations in: {ns}/{FLEETCAST_DEP}")
#     patch = {
#         "spec": {
#             "template": {
#                 "metadata": {
#                     "annotations": {
#                         "prometheus.io/scrape": "true",
#                         "prometheus.io/path": "/metrics",
#                         "prometheus.io/port": f"{FLEETCAST_METRICS_PORT}",
#                     }
#                 }
#             }
#         }
#     }
#     run_cmd(
#         ["kubectl", "-n", ns, "patch", "deploy", FLEETCAST_DEP, "--type=merge", "-p", json.dumps(patch)],
#         check=False
#     )
#     run_cmd(f"kubectl -n {shlex.quote(ns)} rollout restart deploy/{shlex.quote(FLEETCAST_DEP)}",
#             shell=True, check=False)
#     run_cmd(f"kubectl -n {shlex.quote(ns)} rollout status  deploy/{shlex.quote(FLEETCAST_DEP)}",
#             shell=True, check=False)

# test: kubectl -n observe exec deploy/prometheus-server -c prometheus-server -- \ wget -qO- "http://localhost:9090/api/v1/query?query=http_requests_total{namespace=\"fleetcast\"}"
def main():
    ensure_ns(PROM_NAMESPACE)
    helm_repo_setup()
    helm_apply_values()
    print("[debug] finished helm, moving on")

    check_configmap_for_duplicate_global()
    print("[debug] boutta wait")

    wait_for_prometheus_ready()

    check()
    ensure_fleetcast_scrape_annotations()


if __name__ == "__main__":
    main()
