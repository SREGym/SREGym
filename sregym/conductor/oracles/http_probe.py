"""Send HTTP requests to an in-cluster Service from a pod that already exists.

Shared by oracles that grade real requests: no ephemeral pod and no Prometheus dependency.
"""

import re
import shlex


def find_probe_pod(kubectl, namespace: str) -> str | None:
    """Pick a running pod to probe from, preferring consul.

    consul is always present in hotel-reservation and ships wget. Otherwise any
    running pod that is neither the frontend nor the workload generator.
    """
    pods = kubectl.list_pods(namespace).items
    running = [pod.metadata.name for pod in pods if pod.status.phase == "Running" and pod.metadata.name]
    for name in running:
        if name.startswith("consul"):
            return name
    for name in running:
        if "frontend" not in name and "wrk2" not in name:
            return name
    return None


def wget_from_pod(kubectl, pod: str, namespace: str, url: str) -> tuple[list[str], str]:
    """GET ``url`` from inside ``pod``; return (HTTP status codes seen, raw wget output).

    wget exits nonzero for HTTP errors. Its response is preserved inside the
    pod, without masking a failure to execute the command through Kubernetes.
    """
    script = f"wget -T 10 -S -O - {shlex.quote(url)} 2>&1 || true"
    cmd = f"kubectl exec {shlex.quote(pod)} -n {shlex.quote(namespace)} -- sh -c {shlex.quote(script)}"
    output = kubectl.exec_command_checked(cmd, timeout=30)
    return re.findall(r"^\s*HTTP/\S+\s+(\d{3})\b", output, re.MULTILINE), output
