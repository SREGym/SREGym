"""Read-only diagnostics; all artifacts live outside Loki and application containers."""

import concurrent.futures
import json
import subprocess
import time
from pathlib import Path

# Runs in the kind node's cgroup, never launches a diagnostic JVM in a service.
CGROUP_SCRIPT = r"""
find /sys/fs/cgroup -type d -name 'cri-containerd-*.scope' | while IFS= read -r d; do
  echo "CGROUP $d"
  for f in memory.current memory.peak memory.max memory.events memory.stat memory.swap.current; do
    if [ -r "$d/$f" ]; then
      echo "FILE $f"
      cat "$d/$f"
    fi
  done
done
"""
HTTP_PROBE = """import json,urllib.request
out={}
for name,url in [('products','http://frontend-proxy:8080/api/products'),('workload','http://127.0.0.1:8089/stats/requests')]:
    try:
        with urllib.request.urlopen(url,timeout=5) as r:
            body=json.load(r)
            out[name]={'status':r.status}
            if name=='products': out[name]['product_count']=len(body) if isinstance(body,list) else None
            else:
                for key in ['state','total_rps','fail_ratio','user_count']: out[name][key]=body.get(key)
                out[name]['aggregate']=[s for s in body.get('stats',[]) if s.get('name')=='Aggregated']
    except Exception as e: out[name]={'error':str(e)}
print(json.dumps(out))
"""


def run(args, timeout=20):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return {"rc": p.returncode, "out": p.stdout, "err": p.stderr}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"rc": -1, "error": str(exc)}


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def parse_cgroups(output):
    groups = {}
    current = None
    field = None
    for line in output.splitlines():
        if line.startswith("CGROUP "):
            cid = line.rsplit("/", 1)[-1].removeprefix("cri-containerd-").removesuffix(".scope")
            current = groups.setdefault(cid, {})
            field = None
        elif line.startswith("FILE ") and current is not None:
            field = line[5:]
            current[field] = ""
        elif current is not None and field:
            current[field] += line + "\n"
    return groups


def pod_findings(pods):
    """Include init containers and completed/replaced pod identities in evidence."""
    if not pods["items"]:
        raise ValueError("No pods returned")
    findings = []
    for pod in pods["items"]:
        meta, status = pod["metadata"], pod.get("status", {})
        identity = {"namespace": meta["namespace"], "pod": meta["name"], "uid": meta["uid"]}
        # Normal deployment removes the bundled Jaeger pod asynchronously.
        # Ignore readiness of desired deletions, but retain their OOM evidence.
        deleting = bool(meta.get("deletionTimestamp"))
        if not deleting and status.get("phase") not in ("Running", "Succeeded"):
            findings.append({**identity, "reason": "pod_not_running"})
        if (
            not deleting
            and status.get("phase") == "Running"
            and not any(c["type"] == "Ready" and c["status"] == "True" for c in status.get("conditions", []))
        ):
            findings.append({**identity, "reason": "pod_not_ready"})
        for key in ("initContainerStatuses", "containerStatuses", "ephemeralContainerStatuses"):
            for container in status.get(key, []):
                base = {**identity, "container": container["name"], "restarts": container.get("restartCount", 0)}
                for state in ("state", "lastState"):
                    termination = container.get(state, {}).get("terminated", {})
                    if termination.get("reason") == "OOMKilled":
                        findings.append({**base, "reason": "OOMKilled", "termination": termination})
                if container.get("restartCount", 0):
                    findings.append({**base, "reason": "container_restarted"})
    return findings


def collect(context, kind_nodes=(), traffic=False):
    prefix = ["kubectl", "--context", context, "--request-timeout=15s"]
    commands = {
        "pods": prefix + ["get", "pods", "-A", "-o", "json"],
        "nodes": prefix + ["get", "nodes", "-o", "json"],
        "events": prefix + ["get", "events", "-A", "-o", "json"],
        "metrics": prefix + ["top", "pods", "-A", "--containers"],
        "node_metrics": prefix + ["top", "nodes"],
    }
    for node in kind_nodes:
        commands["cgroup/" + node] = ["docker", "exec", node, "sh", "-c", CGROUP_SCRIPT]
    if traffic:
        commands["http_probe"] = prefix + [
            "-n",
            "astronomy-shop",
            "exec",
            "deploy/load-generator",
            "-c",
            "load-generator",
            "--",
            "python",
            "-c",
            HTTP_PROBE,
        ]
    data = {"time": time.time()}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {name: pool.submit(run, args) for name, args in commands.items()}
        for name, future in futures.items():
            result = future.result()
            data[name] = result
            if name.startswith("cgroup/") and result["rc"] == 0:
                result["containers"] = parse_cgroups(result.pop("out"))
    for name in ("meminfo", "pressure/memory"):
        path = Path("/proc") / name
        data[name] = path.read_text() if path.exists() else None
    try:
        data["findings"] = pod_findings(json.loads(data["pods"]["out"]))
    except (KeyError, ValueError, TypeError) as exc:
        data["collection_error"] = str(exc)
    return data


def capture_attempt(directory, phase, *, context=None):
    """Capture a phase before mutation; post-injection findings are diagnostic only."""
    if context is None:
        current = run(["kubectl", "config", "current-context"])
        if current["rc"] or not current.get("out", "").strip():
            raise RuntimeError("Cannot determine Kubernetes context for baseline diagnostics")
        context = current["out"].strip()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    data = collect(context)
    data["phase"] = phase
    destination = directory / f"{phase}.json"
    if destination.exists():
        destination = directory / f"{phase}-{time.time_ns()}.json"
    save(destination, data)
    errors = [finding for finding in data.get("findings", []) if finding["reason"] != "container_restarted"]
    if data["pods"]["rc"] or "collection_error" in data:
        errors.append({"reason": "pod_collection_failed"})
    return errors
