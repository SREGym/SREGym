"""Problem: init container stuck waiting on a non-existent dependency.

Real-world story
----------------
Init containers that wait for a dependency to become reachable — the classic
`wait-for-it.sh` / `until nslookup <dep>; do sleep 5; done` pattern documented
in the Kubernetes init-container docs — are widely used to enforce ordering
between microservices and to gate startup on a downstream DB or sidecar.  The
failure mode is that a recent refactor renames or removes the dependency
service while leaving the dependent deployment's init-container manifest
pointing at the old DNS name.  The init container then loops forever, the pod
stays in `Init:0/1`, and the deployment never finishes rolling out.  Recent
postmortems and r/kubernetes threads cite this as one of the most common
"silent" mid-rollout incidents because nothing crashes — there are simply no
ready replicas of the affected service.

This problem is **distinct from existing entries** in the registry:

* ``rolling_update_misconfigured_*`` injects a `sleep infinity` init container
  on a *synthetic* `python:3.9-slim` deployment as a mechanism to wedge a
  pathological `maxUnavailable: 100% / maxSurge: 0%` rolling-update strategy;
  the diagnosed root cause there is the strategy, not the init container.
* ``rbac_misconfiguration`` makes an init container fail because of
  insufficient *permissions* (RBAC denial), not because it is hanging.

Here the diagnosed root cause is the dependency-wait init container itself,
patched onto a real application microservice.
"""

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class InitContainerDependencyHang(Problem):
    """Injects a busybox init container that loops on `nslookup` against a
    non-existent service.  Pods of the target deployment never leave
    `Init:0/1`, the new ReplicaSet has zero ready replicas, and the rollout
    stalls.  Mitigation requires either removing the broken init container or
    pointing it at a real (or always-resolvable) dependency.
    """

    def __init__(self, app_name: str = "hotel_reservation", faulty_service: str = "frontend"):
        self.app_name = app_name
        self.faulty_service = faulty_service

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
        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"Deployment `{self.faulty_service}` in namespace `{self.namespace}` has an "
                f"init container `wait-for-legacy-config` that runs "
                f"`until nslookup legacy-config-service.{self.namespace}.svc.cluster.local; "
                f"do sleep 5; done`. The Service `legacy-config-service` does not exist in "
                f"the cluster (it was removed/renamed in a prior refactor), so the init "
                f"container's nslookup loop never succeeds. Every new pod of "
                f"`{self.faulty_service}` is therefore stuck in `Init:0/1` and the rollout "
                f"stalls indefinitely — the deployment shows zero updated/ready replicas "
                f"for the new ReplicaSet while the old ReplicaSet's pods continue serving "
                f"traffic. Mitigation: remove the broken init container from the deployment "
                f"spec, or repoint it at a real service that resolves."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()

        # All pods Running is sufficient here: the broken init container wedges
        # every pod the ReplicaSet schedules, so deleting a stuck pod just gets
        # a fresh stuck one, and deleting the namespace leaves no pods at all —
        # both fail this check. A pass therefore means the init container was
        # genuinely fixed.
        self.mitigation_oracle = MitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.inject_init_container_dependency_hang(microservices=[self.faulty_service])
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.recover_init_container_dependency_hang(microservices=[self.faulty_service])
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
