"""Stale hostAliases DNS Poisoning problem for Astronomy Shop."""

import logging
import time

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class StaleHostAliasesDNSPoisoningAstronomyShop(Problem):
    """Fault injection problem simulating stale hostAliases DNS poisoning in Astronomy Shop."""

    def __init__(self):
        self.app = AstronomyShop()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)

        self.kubectl = KubeCtl()
        self.faulty_service = "frontend"
        self.target_backend = "productcatalogservice"
        self.blackhole_ip = "127.0.0.1"

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"The {self.faulty_service} deployment contains a stale hostAliases entry explicitly mapping "
                f"{self.target_backend} to {self.blackhole_ip}. Since /etc/hosts takes precedence over CoreDNS, "
                f"this pod-local override silently shadows the correct in-cluster DNS, causing immediate ECONNREFUSED "
                f"errors, even though CoreDNS and the backend service are perfectly healthy."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.app.create_workload()
        self.mitigation_oracle = MitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        """Inject stale hostAliases DNS poisoning into frontend deployment."""
        print("== Fault Injection ==")
        logger.info(
            "Injecting stale hostAliases DNS poisoning into %s deployment targeting %s -> %s",
            self.faulty_service,
            self.target_backend,
            self.blackhole_ip,
        )
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._inject(
            fault_type="stale_hostaliases",
            microservices=[self.faulty_service],
            target_host=self.target_backend,
            blackhole_ip=self.blackhole_ip,
        )
        print(f"Injected stale_hostaliases fault into {self.faulty_service} | Namespace: {self.namespace}\n")
        time.sleep(30)

    @mark_fault_injected
    def recover_fault(self):
        """Recover from stale hostAliases DNS poisoning fault."""
        print("== Fault Recovery ==")
        logger.info("Recovering stale hostAliases DNS poisoning fault from %s deployment", self.faulty_service)
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._recover(
            fault_type="stale_hostaliases",
            microservices=[self.faulty_service],
        )
        print(f"Recovered stale_hostaliases fault from {self.faulty_service} | Namespace: {self.namespace}\n")
        time.sleep(30)
