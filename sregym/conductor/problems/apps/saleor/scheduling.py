"""Saleor: scheduling."""

from __future__ import annotations

from sregym.conductor.oracles.assign_non_existent_node_mitigation import AssignNonExistentNodeMitigationOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.apps.saleor.helpers import API, API_SERVICE, APP, WORKER
from sregym.conductor.problems.assign_non_existent_node import AssignNonExistentNode
from sregym.conductor.problems.environment import configure_port
from sregym.conductor.problems.pvc_claim_mismatch import PVCClaimMismatch
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector


class AssignNonExistentNodeSaleor(AssignNonExistentNode):
    def __init__(self, faulty_service: str = API):
        self.faulty_service = faulty_service
        configure_port(
            self,
            APP,
            component=faulty_service,
            description=(
                f"Deployment `{faulty_service}` (Saleor's GraphQL API, the only backend of Service "
                f"`{API_SERVICE}`) has a `nodeSelector` `kubernetes.io/hostname: extra-node` that matches no node in "
                "the cluster, so the scheduler cannot place its pod. The pod stays Pending with a node-affinity/"
                "selector mismatch (FailedScheduling) and never becomes Ready, the Service has no endpoints, and "
                "every storefront/checkout GraphQL request fails."
            ),
            oracle_factory=AssignNonExistentNodeMitigationOracle,
        )


class PVCClaimMismatchSaleor(PVCClaimMismatch):
    def __init__(self):
        self.faulty_service = [API, WORKER]
        configure_port(
            self,
            APP,
            component="saleor-media",
            description=(
                f"Deployments `{API}` and `{WORKER}` reference a non-existent PersistentVolumeClaim "
                "(`saleor-media-broken` instead of `saleor-media`) for their shared `/app/media` volume. The "
                "scheduler cannot bind the claim, so both pods stay Pending (`persistentvolumeclaim "
                '"saleor-media-broken" not found`) and never start: the GraphQL API has no endpoints and Celery '
                "tasks are not processed, although the real `saleor-media` claim is Bound and healthy."
            ),
            oracle_factory=MitigationOracle,
        )
        self.injector = VirtualizationFaultInjector(namespace=self.namespace)
