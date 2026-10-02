"""A minimal problem for checking that a Harbor environment can run SREGym.

Real SREGym problems deploy observability stacks and large applications, so a
misconfigured provider can take a long time to fail. This session exercises
the same Harbor path in a few minutes: the privileged sidecar, the per-run KIND
cluster, SREGym's filtered Kubernetes API proxy, the healthcheck, the oracle
token, collect-hook grading and the separate verifier.

The fault points a Service's selector at no pods; the oracle requires the
Deployment to be available and the Service to have ready endpoints.
"""

import json
import logging
import subprocess
import time

from sregym.service.k8s_proxy import KubernetesAPIProxy

logger = logging.getLogger("all.sregym.harbor.selftest")

PROBLEM_ID = "harbor_selftest"
APP_NAME = "Harbor Self-Test"
APP_DESCRIPTION = "A single nginx web server behind a Kubernetes Service named web."
NAMESPACE = "sregym-selftest"
IMAGE = "nginx:1.27-alpine"
BROKEN_SELECTOR = {"app": "web-missing"}
GRADE_TIMEOUT_S = 120


def _kubectl(*args: str, input_data: str | None = None) -> str:
    return subprocess.run(
        ["kubectl", *args], input=input_data, capture_output=True, text=True, check=True, timeout=600
    ).stdout


class SelfTestSession:
    def __init__(self, *, advertise_host: str, proxy_port: int):
        self.proxy = KubernetesAPIProxy(listen_host="0.0.0.0", listen_port=proxy_port, advertise_host=advertise_host)

    def setup(self) -> str:
        _kubectl("create", "namespace", NAMESPACE)
        _kubectl("-n", NAMESPACE, "create", "deployment", "web", f"--image={IMAGE}")
        _kubectl("-n", NAMESPACE, "expose", "deployment", "web", "--port=80")
        _kubectl("-n", NAMESPACE, "rollout", "status", "deployment/web", "--timeout=540s")
        patch = json.dumps({"spec": {"selector": BROKEN_SELECTOR}})
        _kubectl("-n", NAMESPACE, "patch", "service", "web", "--type=merge", f"--patch={patch}")
        logger.info("[HARBOR] Self-test fault injected")
        self.proxy.start()
        return self.proxy.generate_agent_kubeconfig()

    def recover(self) -> None:
        # A JSON patch replaces the selector instead of merging into it.
        patch = json.dumps([{"op": "replace", "path": "/spec/selector", "value": {"app": "web"}}])
        _kubectl("-n", NAMESPACE, "patch", "service", "web", "--type=json", f"--patch={patch}")

    def grade(self) -> dict:
        deadline = time.monotonic() + GRADE_TIMEOUT_S
        while True:
            deployment = json.loads(_kubectl("-n", NAMESPACE, "get", "deployment", "web", "-o", "json"))
            slices = json.loads(
                _kubectl("-n", NAMESPACE, "get", "endpointslices", "-l", "kubernetes.io/service-name=web", "-o", "json")
            )
            ready = sum(
                1
                for item in slices["items"]
                for endpoint in item.get("endpoints") or []
                if endpoint.get("conditions", {}).get("ready")
            )
            available = deployment.get("status", {}).get("availableReplicas") or 0
            if ready and available:
                return {"success": True, "ready_endpoints": ready}
            if time.monotonic() >= deadline:
                return {
                    "success": False,
                    "reason": "service_has_no_ready_endpoints" if available else "deployment_unavailable",
                    "ready_endpoints": ready,
                    "available_replicas": available,
                }
            time.sleep(5)

    def close(self) -> None:
        self.proxy.stop()
