"""Pod-local hosts override shadowing in-cluster DNS on Astronomy Shop.

A leftover `hostAliases` entry — the kind teams add during a migration to pin a
hostname at a fixed address and then forget to remove — makes the frontend
resolve its product-catalog backend to a dead address. Because the libc
resolver reads /etc/hosts before it queries CoreDNS, the entry wins forever and
the failure is confined to one Deployment's pods.

Every control-plane signal stays green: pods are Running and Ready, CoreDNS is
healthy, the backend Service has live Endpoints, and `nslookup` from any other
pod resolves correctly. Only the frontend's own requests fail.
"""

import logging

from sregym.conductor.oracles.compound import CompoundedOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.oracles.stale_hostaliases_mitigation import StaleHostAliasesMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class StaleHostAliasesDNSPoisoningAstronomyShop(Problem):
    """Poison one Deployment's /etc/hosts so it cannot reach its backend."""

    FAULTY_SERVICE = "frontend"
    # The frontend reaches the catalog through PRODUCT_CATALOG_ADDR, which the
    # chart sets to `product-catalog:8080`. The alias must use exactly that
    # hostname — any other spelling resolves normally and the fault is a no-op.
    TARGET_BACKEND = "product-catalog"
    # Loopback gives an immediate connection error rather than a long TCP
    # timeout: the frontend's own server binds the pod IP (Next.js standalone
    # binds $HOSTNAME), so nothing is listening on 127.0.0.1:8080.
    BLACKHOLE_IP = "127.0.0.1"

    def __init__(self):
        self.app = AstronomyShop()
        super().__init__(app=self.app)

        self.kubectl = KubeCtl()
        self.faulty_service = self.FAULTY_SERVICE
        self.target_backend = self.TARGET_BACKEND
        self.blackhole_ip = self.BLACKHOLE_IP
        self.injector = VirtualizationFaultInjector(namespace=self.namespace)

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"The {self.faulty_service} deployment carries a stale hostAliases entry that maps "
                f"{self.target_backend} to {self.blackhole_ip}, so the kubelet writes that mapping into the pod's "
                "/etc/hosts. The libc resolver consults /etc/hosts before CoreDNS, so the pod-local override "
                "silently shadows in-cluster DNS and every call to the backend fails with a connection error, "
                "even though CoreDNS, the backend Service, and its Endpoints are all healthy."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        # The generic oracle keeps namespace-wide health honest (nothing deleted
        # or scaled to zero); the dedicated oracle proves the override is gone
        # and that catalog traffic actually flows again.
        self.mitigation_oracle = CompoundedOracle(
            self,
            MitigationOracle(problem=self),
            StaleHostAliasesMitigationOracle(problem=self),
        )

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        logger.info(
            "Pinning %s to %s in the hosts file of the %s deployment",
            self.target_backend,
            self.blackhole_ip,
            self.faulty_service,
        )
        self.injector.inject_stale_hostaliases(
            microservices=[self.faulty_service],
            target_host=self.target_backend,
            blackhole_ip=self.blackhole_ip,
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        logger.info("Removing the hosts override from the %s deployment", self.faulty_service)
        self.injector.recover_stale_hostaliases(microservices=[self.faulty_service])
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
