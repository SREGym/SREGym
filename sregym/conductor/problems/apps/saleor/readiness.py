"""Saleor: readiness."""

from __future__ import annotations

import copy

from sregym.conductor.oracles.sustained_readiness import SustainedReadinessOracle
from sregym.conductor.problems.apps.saleor.helpers import API, API_SERVICE, APP, _replace_deployment
from sregym.conductor.problems.environment import configure_port
from sregym.conductor.problems.liveness_probe_too_aggressive import LivenessProbeTooAggressive
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.utils.decorators import mark_fault_injected


class LivenessProbeTooAggressiveSaleor(LivenessProbeTooAggressive):
    """The original's aggressive probe on saleor-api (uvicorn needs seconds to import Django and bind).

    The original first swapped in a deliberately slow custom service; Saleor's
    API is slow to start on its own, so the probe change is applied to it as is.
    """

    def __init__(self, faulty_service: str = API):
        self.faulty_service = faulty_service
        configure_port(
            self,
            APP,
            component=faulty_service,
            description=(
                f"Deployment `{faulty_service}` uses an overly aggressive liveness probe (tcpSocket 8000 with "
                "`initialDelaySeconds=0`, `periodSeconds=1`, `failureThreshold=1`) and "
                "`terminationGracePeriodSeconds=0`. uvicorn needs several seconds to import Saleor/Django and bind "
                "port 8000, so the first probe fails and the kubelet kills the container before it can serve; the "
                f"pod restarts in a loop (CrashLoopBackOff) and never stays Ready, leaving Service `{API_SERVICE}` "
                "without endpoints and API requests failing."
            ),
            oracle_factory=lambda problem: SustainedReadinessOracle(problem=problem, sustained_period=30),
        )
        self.injector = VirtualizationFaultInjector(namespace=self.namespace)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = self.injector
        deployment = injector._get_deployment_yaml(self.faulty_service)
        injector._write_yaml_to_file(self.faulty_service, copy.deepcopy(deployment))
        pod_spec = deployment["spec"]["template"]["spec"]
        for container in pod_spec["containers"]:
            probe = container.get("livenessProbe")
            if probe:
                probe["initialDelaySeconds"] = 0
                probe["periodSeconds"] = 1
                probe["failureThreshold"] = 1
        pod_spec["terminationGracePeriodSeconds"] = 0
        _replace_deployment(injector, self.faulty_service, deployment)
        injector.kubectl.wait_for_stable(self.namespace)
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
