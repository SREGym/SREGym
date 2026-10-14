"""Slack Spine: readiness."""

from __future__ import annotations

from sregym.conductor.oracles.compound import CompoundedOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.oracles.readiness_probe_mitigation import ReadinessProbeMitigationOracle
from sregym.conductor.oracles.sustained_readiness import SustainedReadinessOracle
from sregym.conductor.problems.apps.slack_spine.manifests import APP
from sregym.conductor.problems.environment import (
    configure_port,
)
from sregym.conductor.problems.init_container_dependency_hang import InitContainerDependencyHang
from sregym.conductor.problems.liveness_probe_misconfiguration import LivenessProbeMisconfiguration
from sregym.conductor.problems.readiness_probe_misconfiguration import ReadinessProbeMisconfiguration
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector


class LivenessProbeMisconfigurationSlack(LivenessProbeMisconfiguration):
    def __init__(self, faulty_service: str = "svc-workspace"):
        self.faulty_service = faulty_service
        self.app_name = APP
        configure_port(
            self,
            APP,
            component=faulty_service,
            description=(
                f"The deployment `{faulty_service}` has a misconfigured liveness probe that checks `/healthz` on port "
                "`8080` (the service listens on 8000) with failureThreshold 1, so Kubernetes kills and restarts its "
                "pod every few seconds and it ends in CrashLoopBackOff. svc-channel revalidates the org policy on "
                "svc-workspace for every message send, so sends fail intermittently or completely while the pod churns."
            ),
            # Between kills the pod is briefly Ready, so a single snapshot can
            # miss the restart loop: readiness must also hold for a minute.
            oracle_factory=lambda problem: CompoundedOracle(
                problem,
                MitigationOracle(problem),
                SustainedReadinessOracle(problem, buffer_period=30, sustained_period=60),
            ),
        )
        self.injector = VirtualizationFaultInjector(namespace=self.namespace)


class InitContainerDependencyHangSlack(InitContainerDependencyHang):
    """Faithful to the original: the new ReplicaSet hangs in Init while the old pod keeps serving."""

    def __init__(self, faulty_service: str = "svc-file"):
        self.faulty_service = faulty_service
        self.app_name = APP
        configure_port(
            self,
            APP,
            component=f"deployment/{faulty_service}",
            description=(
                f"Deployment `{faulty_service}` has an init container `wait-for-legacy-config` that runs `until "
                "nslookup legacy-config-service.<namespace>.svc.cluster.local; do sleep 5; done`. The Service "
                "`legacy-config-service` does not exist, so the loop never succeeds: every new pod is stuck in "
                "`Init:0/1` and the rollout stalls with zero updated/ready replicas in the new ReplicaSet while the "
                "old ReplicaSet's pod continues serving traffic. Mitigation: remove the broken init container or "
                "repoint it at a service that resolves."
            ),
            oracle_factory=MitigationOracle,
        )


class ReadinessProbeMisconfigurationIA(ReadinessProbeMisconfiguration):
    def __init__(self, app_name: str = "slack_spine", faulty_service: str = "svc-message"):
        self.faulty_service = faulty_service
        configure_port(
            self,
            app_name,
            component=faulty_service,
            description=(
                f"The deployment `{faulty_service}` has a misconfigured readiness probe that targets a non-existent "
                "health endpoint (`/healthz` on port `8080`; the service listens on 8000), so its pods fail "
                "readiness checks and remain NotReady. Kubernetes excludes them from the Service endpoints even "
                "though the containers keep running, so every request path through this service fails."
            ),
            oracle_factory=ReadinessProbeMitigationOracle,
        )
