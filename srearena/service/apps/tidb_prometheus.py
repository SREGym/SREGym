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

PROM_PORT         = 9090         
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
    """
    run_cmd([
        "helm", "upgrade", PROM_RELEASE, PROM_CHART,
        "-n", PROM_NAMESPACE, "--install", "--reset-values",
        "-f", str(PROM_VALUES_PATH), 
        "--set", "server.service.type=NodePort",
        "--set", f"server.service.nodePort={DESIRED_NODEPORT}",
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


def get_service_json(name: str) -> dict:
    out = run_cmd(["kubectl", "-n", PROM_NAMESPACE, "get", "svc", name, "-o", "json"], capture=True)
    return json.loads(out)


def ensure_nodeport(service_name: str, target_port: int, node_port: int):
    run_cmd([
        "kubectl", "-n", PROM_NAMESPACE, "patch", "svc", service_name,
        "--type=json",
        "-p=[{\"op\":\"replace\",\"path\":\"/spec/type\",\"value\":\"NodePort\"}]"
    ], check=False)

    svc = get_service_json(service_name)
    ports = svc.get("spec", {}).get("ports", [])
    if not ports:
        raise RuntimeError(f"Service {service_name} has no ports to patch")

    target_idx = None
    for i, p in enumerate(ports):
        tp = p.get("targetPort", p.get("port"))
        if (isinstance(tp, int) and tp == target_port) or (isinstance(tp, str) and str(tp) == str(target_port)) \
           or (p.get("port") == target_port):
            target_idx = i; break
    if target_idx is None:
        target_idx = 0

    patch_ops = []
    if str(ports[target_idx].get("targetPort")) != str(target_port):
        patch_ops.append({"op":"replace","path":f"/spec/ports/{target_idx}/targetPort","value":target_port})

    try:
        run_cmd(["kubectl","-n",PROM_NAMESPACE,"patch","svc",service_name,"--type=json",
                 "-p="+json.dumps(patch_ops+[{"op":"replace","path":f"/spec/ports/{target_idx}/nodePort","value":node_port}])])
    except subprocess.CalledProcessError:
        run_cmd(["kubectl","-n",PROM_NAMESPACE,"patch","svc",service_name,"--type=json",
                 "-p="+json.dumps(patch_ops+[{"op":"add","path":f"/spec/ports/{target_idx}/nodePort","value":node_port}])])

    svc2 = get_service_json(service_name)
    np = svc2["spec"]["ports"][target_idx].get("nodePort")
    print(f"Service {service_name} now NodePort={np} (wanted {node_port})")


def list_ready_node_ips() -> List[str]:
    out = run_cmd(["kubectl", "get", "nodes", "-o", "json"], capture=True)
    data = json.loads(out)
    ips = []
    for n in data.get("items", []):
        conds = n.get("status", {}).get("conditions", [])
        if not any(c.get("type")=="Ready" and c.get("status")=="True" for c in conds):
            continue
        addrs = n.get("status", {}).get("addresses", [])
        ext = [a["address"] for a in addrs if a["type"]=="ExternalIP"]
        intr = [a["address"] for a in addrs if a["type"]=="InternalIP"]
        ips.append(ext[0] if ext else intr[0])
    return ips


def find_first_reachable_prom_url(node_port: int, timeout=RETRY_SECS) -> Optional[str]:
    ips = list_ready_node_ips()
    print(f"Candidate node IPs: {ips}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        for ip in ips:
            url = f"http://{ip}:{node_port}"
            try:
                run_cmd(f"curl -sS {shlex.quote(url)}/-/ready", shell=True, check=True)
                print(f"Prometheus is reachable at {url}")
                return url
            except subprocess.CalledProcessError:
                continue
        time.sleep(SLEEP_BETWEEN)
    return None


def jq_exists() -> bool:
    return subprocess.run(["bash", "-lc", "command -v jq >/dev/null 2>&1"]).returncode == 0


def find_dep_namespace(hint_name: str) -> Optional[str]:
    try:
        ns = run_cmd(
            "kubectl get deploy -A -o jsonpath='{range .items[*]}{.metadata.namespace} {.metadata.name}{\"\\n\"}{end}' "
            f"| awk '$2==\"{hint_name}\"{{print $1; exit}}'",
            shell=True, capture=True
        )
        return ns if ns else None
    except subprocess.CalledProcessError:
        return None


# def annotate_fleetcast_and_rollout():
#     """
#     Ensure the fleetcast Deployment's *pod template* carries Prometheus annotations,
#     then restart it so pods are recreated with those annotations.
#     """
#     ns = FLEETCAST_NS if ns_exists(FLEETCAST_NS) else find_dep_namespace(FLEETCAST_DEP)
#     if not ns:
#         print(f"(skip) Could not find deployment '{FLEETCAST_DEP}' to annotate.")
#         return

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


def check(prom_url: str):
    run_cmd(["kubectl","-n",PROM_NAMESPACE,"get","svc",PROM_SVC_NAME])
    run_cmd(f"curl -sS {prom_url}/-/ready", shell=True)
    if jq_exists():
        run_cmd(
            f"curl -sS {prom_url}/api/v1/targets | "
            "jq -r '.data.activeTargets[] | "
            "select(.labels.job==\"kubernetes-pods\") | "
            "(.labels.namespace + \" \" + .labels.pod + \" \" + .health)' | sort -u",
            shell=True
        )
    else:
        run_cmd(f"curl -sS {prom_url}/api/v1/targets", shell=True)


def verify_fleetcast(prom_url: str):
    q1 = "count(up{job=\"kubernetes-pods\",namespace=\"%s\"}==1)" % FLEETCAST_NS
    q2 = "sum(scrape_samples_scraped{job=\"kubernetes-pods\",namespace=\"%s\"})" % FLEETCAST_NS
    out1 = run_cmd(f"curl -sS --get '{prom_url}/api/v1/query' --data-urlencode 'query={q1}'", shell=True, capture=True)
    out2 = run_cmd(f"curl -sS --get '{prom_url}/api/v1/query' --data-urlencode 'query={q2}'", shell=True, capture=True)
    try:
        r1 = json.loads(out1); r2 = json.loads(out2)
        up = int(float(r1.get("data",{}).get("result",[{"value":[0,"0"]}])[0]["value"][1])) if r1.get("data",{}).get("result") else 0
        samples = int(float(r2.get("data",{}).get("result",[{"value":[0,"0"]}])[0]["value"][1])) if r2.get("data",{}).get("result") else 0
    except Exception:
        up = 0; samples = 0
    print(f"fleetcast targets_up={up} samples_scraped={samples}")
    return up, samples


def main():
    ensure_ns(PROM_NAMESPACE)
    helm_repo_setup()
    helm_apply_values()

    check_configmap_for_duplicate_global()
    wait_for_prometheus_ready()

    try:
        run_cmd(["kubectl", "-n", PROM_NAMESPACE, "get", "svc", PROM_SVC_NAME])
    except subprocess.CalledProcessError:
        print("Could not find expected Service. Available services in namespace:")
        run_cmd(["kubectl", "-n", PROM_NAMESPACE, "get", "svc", "-o", "wide"], check=False)
        raise

    ensure_nodeport(PROM_SVC_NAME, target_port=PROM_PORT, node_port=DESIRED_NODEPORT)

    prom_url = find_first_reachable_prom_url(DESIRED_NODEPORT) or "http://localhost:9090"
    print(f"[info] Using Prometheus URL: {prom_url}")

    #annotate_fleetcast_and_rollout()

    time.sleep(8)

    check(prom_url)
    verify_fleetcast(prom_url)


if __name__ == "__main__":
    main()
