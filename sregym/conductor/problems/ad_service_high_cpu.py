"""Otel demo adServiceHighCpu feature flag fault."""

import json

from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.alert_oracle import AlertOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_otel import OtelFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class AdServiceHighCpu(Problem):
    def __init__(self):
        super().__init__(app=AstronomyShop())
        self.kubectl = KubeCtl()
        self.injector = OtelFaultInjector(namespace=self.namespace)
        self.faulty_service = "ad"
        self.feature_flag = "adHighCpu"
        self.cpu_limit = "100m"
        self.root_cause = self.build_structured_root_cause(
            component=self.faulty_service,
            namespace=self.namespace,
            description=(
                f"The `{self.faulty_service}` deployment is CPU-throttled due to a tight CPU limit "
                f"(`{self.cpu_limit}`) combined with artificially elevated CPU usage, causing sustained high CPU usage and performance "
                "degradation. Pods for the ad service show increased request latency and intermittent timeouts "
                "under normal storefront traffic. This appears as slower page loads and ad content delays even "
                "when other services remain healthy."
            ),
        )
        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = AlertOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector.inject_fault(self.feature_flag)
        # Set a tight CPU limit so the high-CPU flag causes observable throttling.
        patch = json.dumps(
            {
                "spec": {
                    "template": {
                        "spec": {"containers": [{"name": "ad", "resources": {"limits": {"cpu": self.cpu_limit}}}]}
                    }
                }
            }
        )
        self.kubectl.exec_command_checked(
            f"kubectl patch deployment {self.faulty_service} -n {self.namespace} --type=strategic -p '{patch}'",
            timeout=30,
        )
        applied = self.kubectl.exec_command_checked(
            f"kubectl get deployment {self.faulty_service} -n {self.namespace} "
            "-o jsonpath='{.spec.template.spec.containers[?(@.name==\"ad\")].resources.limits.cpu}'",
            timeout=30,
        )
        applied = applied.strip()
        if applied != self.cpu_limit:
            raise RuntimeError(
                f"ad cpu limit patch did not take: expected {self.cpu_limit}, deployment reads {applied!r}"
            )
        self._wait_for_limited_pods()
        print(f"Fault: AdServiceHighCpu | Namespace: {self.namespace}\n")

    def _wait_for_limited_pods(self):
        self.kubectl.exec_command_checked(
            f"kubectl rollout status deployment/{self.faulty_service} -n {self.namespace} --timeout=120s",
            timeout=130,
        )
        pods = json.loads(
            self.kubectl.exec_command_checked(
                f"kubectl get pods -n {self.namespace} -l opentelemetry.io/name={self.faulty_service} -o json",
                timeout=30,
            )
        )
        current = [pod for pod in pods["items"] if not pod["metadata"].get("deletionTimestamp")]
        if not current:
            raise RuntimeError("No current ad pods after the CPU-limit rollout")
        for pod in current:
            container = next((item for item in pod["spec"]["containers"] if item["name"] == "ad"), {})
            status = next((item for item in pod["status"].get("containerStatuses", []) if item["name"] == "ad"), {})
            cpu = container.get("resources", {}).get("limits", {}).get("cpu")
            if (
                pod["status"].get("phase") != "Running"
                or status.get("state", {}).get("running") is None
                or cpu != self.cpu_limit
            ):
                raise RuntimeError(f"Ad pod {pod['metadata']['name']} is not running with CPU limit {self.cpu_limit}")

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_fault(self.feature_flag)
        # A strategic merge removes only this container's CPU limit, even when
        # the agent already removed it or changed the container ordering.
        patch = {
            "spec": {"template": {"spec": {"containers": [{"name": "ad", "resources": {"limits": {"cpu": None}}}]}}}
        }
        try:
            self.kubectl.apps_v1_api.patch_namespaced_deployment(
                name=self.faulty_service, namespace=self.namespace, body=patch, _request_timeout=30
            )
        except ApiException as exc:
            # A removed/replaced original Deployment has no limit to restore.
            if exc.status != 404:
                raise
