"""'Memory limit too low -> OOMKilled CrashLoopBackOff' problem.


Real-world analog
-----------------
A capacity / hardening change sets a container's `resources.limits.memory` far
below its actual working set (the canonical version is "we set limits == requests
and the process grew past it"). The kernel cgroup OOM-kills the process on
startup (exit code 137), Kubernetes restarts it, it OOMs again, and the pod ends
up in CrashLoopBackOff. The service never becomes available even though the
manifest "looks" fine and the image is correct.

Symptoms an agent should see
----------------------------
- Target pod cycling Running -> OOMKilled -> CrashLoopBackOff
- `kubectl describe pod` shows Last State: Terminated, Reason: OOMKilled, exit 137
- `kubectl get deploy` shows the workload with 0 available replicas

Legitimate mitigation
----------------------
Raise (or remove) the container memory limit so it exceeds the working set.
"""

import copy
import logging

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.memory_limit_oomkill_mitigation import (
    MemoryLimitOOMKillMitigationOracle,
)
from sregym.conductor.problems.base import Problem
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class MemoryLimitOOMKill(Problem):
    # A limit guaranteed to be below any real service's startup working set.
    # Verify on your cluster that this reliably OOM-kills the chosen service.
    BROKEN_LIMIT = "12Mi"
    BROKEN_REQUEST = "8Mi"
    # Restored on recovery if the container originally declared no memory limit.
    FALLBACK_LIMIT = "512Mi"

    def __init__(self, app_name: str = "hotel_reservation", faulty_service: str = "mongodb-geo"):
        self.app_name = app_name
        self.faulty_service = faulty_service
        # Convention in these apps: deployment name == service name.
        self.deployment_name = faulty_service

        if app_name == "hotel_reservation":
            self.app = HotelReservation()
        elif app_name == "social_network":
            self.app = SocialNetwork()
        elif app_name == "astronomy_shop":
            self.app = AstronomyShop()
        else:
            raise ValueError(f"Unsupported app name: {app_name}")

        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)
        self.kubectl = KubeCtl()

        # Captured at inject time, consumed at recover time.
        self.container_name = None
        self.original_resources = None
        self.original_replicas = 1

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"The container in the {self.faulty_service} workload has its memory "
                f"limit set far below its working set. The process exceeds the cgroup "
                f"memory limit at startup and is OOM-killed (exit code 137), so the pod "
                f"repeatedly restarts into CrashLoopBackOff and the service never becomes "
                f"available. Fix: raise or remove the container memory limit so it exceeds "
                f"the working set."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = MemoryLimitOOMKillMitigationOracle(problem=self)

    def _find_container(self, dep):
        containers = dep.spec.template.spec.containers
        for c in containers:
            if c.name == self.faulty_service:
                return c
        return containers[0]  # fall back to the first (main) container

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection (memory-limit OOMKill) ==")
        dep = self.kubectl.get_deployment(self.deployment_name, self.namespace)
        self.original_replicas = dep.spec.replicas or 1

        container = self._find_container(dep)
        self.container_name = container.name
        self.original_resources = copy.deepcopy(container.resources.to_dict() if container.resources else {})

        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": self.container_name,
                                "resources": {
                                    "limits": {"memory": self.BROKEN_LIMIT},
                                    "requests": {"memory": self.BROKEN_REQUEST},
                                },
                            }
                        ]
                    }
                }
            }
        }
        self.kubectl.patch_deployment(self.deployment_name, self.namespace, patch)
        print(
            f"Set {self.deployment_name}/{self.container_name} memory limit to "
            f"{self.BROKEN_LIMIT} in namespace {self.namespace}."
        )

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery (memory-limit OOMKill) ==")
        orig = self.original_resources or {}
        orig_limits = orig.get("limits") or {}
        orig_requests = orig.get("requests") or {}

        # Strategic-merge patch on the limits/requests maps preserves any cpu keys.
        restored_limits = {"memory": orig_limits.get("memory", self.FALLBACK_LIMIT)}
        resources = {"limits": restored_limits}
        if orig_requests.get("memory"):
            resources["requests"] = {"memory": orig_requests["memory"]}

        patch = {
            "spec": {"template": {"spec": {"containers": [{"name": self.container_name, "resources": resources}]}}}
        }
        self.kubectl.patch_deployment(self.deployment_name, self.namespace, patch)
        print(f"Restored memory limit on {self.deployment_name}/{self.container_name}.")
