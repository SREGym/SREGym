"""Problem: orphan external readiness gate keeps a healthy frontend Pod out of service.

This models a cloud load-balancer/controller integration failure. In managed Kubernetes
setups, controllers such as GKE NEG or AWS Load Balancer Controller may inject custom
Pod readiness gates so traffic is only routed after external load-balancer health is
confirmed. If the external controller is missing, crashed, or no longer updating the
condition, the container can be running and healthy while the Pod Ready condition remains
False. Kubernetes then keeps the Pod out of Service endpoints, causing user-facing
requests to fail even though logs and container status look mostly normal.
"""

import copy
import time

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.readiness_gate import ReadinessGateMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class ExternalReadinessGateStuck(Problem):
    """Inject a stale external readiness gate into Hotel Reservation's frontend deployment."""

    FAULTY_SERVICE = "frontend"
    CONDITION_TYPE = "cloud.google.com/load-balancer-neg-ready"

    def __init__(self):
        self.app = HotelReservation()
        self.namespace = self.app.namespace
        self.faulty_service = self.FAULTY_SERVICE
        self.condition_type = self.CONDITION_TYPE

        super().__init__(app=self.app, namespace=self.namespace)

        self.kubectl = KubeCtl()
        self.apps_api = client.AppsV1Api()

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"The `{self.faulty_service}` deployment was recreated with a custom Pod readiness gate "
                f"`{self.condition_type}` that is normally set by an external cloud load-balancer or NEG "
                "controller. No controller in the cluster sets this custom Pod condition, so Kubernetes "
                "defaults the missing readiness-gate condition to False. The frontend container can be "
                "Running and internally healthy, but the Pod Ready condition remains False, the Deployment "
                "has zero ready replicas, and the frontend Service has no ready endpoints. The root cause "
                "is an orphan/stale external readiness gate in the frontend Deployment template, not a "
                "bad image, bad port, resource shortage, or application crash. Mitigation is to remove the "
                "orphan readiness gate from the Deployment template or restore the external controller that "
                "sets the condition, then allow the Deployment to roll out a ready frontend pod."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.app.create_workload()
        self.mitigation_oracle = ReadinessGateMitigationOracle(
            problem=self,
            deployment_name=self.faulty_service,
            condition_type=self.condition_type,
            expected_replicas=1,
        )

    def _strip_server_fields(self, deployment):
        deployment = copy.deepcopy(deployment)
        deployment.status = None

        deployment.metadata.resource_version = None
        deployment.metadata.uid = None
        deployment.metadata.creation_timestamp = None
        deployment.metadata.generation = None
        deployment.metadata.managed_fields = None
        deployment.metadata.self_link = None

        return deployment

    def _wait_for_deployment_deleted(self, timeout_seconds: int = 90) -> None:
        deadline = time.monotonic() + timeout_seconds

        while time.monotonic() < deadline:
            try:
                self.apps_api.read_namespaced_deployment(
                    name=self.faulty_service,
                    namespace=self.namespace,
                )
            except ApiException as exc:
                if exc.status == 404:
                    return
                raise

            time.sleep(2)

        raise TimeoutError(
            f"Timed out waiting for deployment/{self.faulty_service} in namespace {self.namespace} to delete"
        )

    def _deployment_with_orphan_readiness_gate(self, deployment):
        deployment = self._strip_server_fields(deployment)

        pod_spec = deployment.spec.template.spec
        readiness_gates = list(pod_spec.readiness_gates or [])

        if not any(gate.condition_type == self.condition_type for gate in readiness_gates):
            readiness_gates.append(client.V1PodReadinessGate(condition_type=self.condition_type))

        pod_spec.readiness_gates = readiness_gates

        annotations = deployment.spec.template.metadata.annotations or {}
        annotations["cloud.google.com/neg"] = '{"ingress": true}'
        annotations["sre.demo/external-readiness-controller"] = "missing"
        deployment.spec.template.metadata.annotations = annotations

        return deployment

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")

        original = self.apps_api.read_namespaced_deployment(
            name=self.faulty_service,
            namespace=self.namespace,
        )
        faulty = self._deployment_with_orphan_readiness_gate(original)

        self.apps_api.delete_namespaced_deployment(
            name=self.faulty_service,
            namespace=self.namespace,
            body=client.V1DeleteOptions(propagation_policy="Foreground"),
        )
        self._wait_for_deployment_deleted()

        self.apps_api.create_namespaced_deployment(
            namespace=self.namespace,
            body=faulty,
        )

        message = f"Recreated deployment/{self.faulty_service} with orphan readiness gate '{self.condition_type}'."
        print(message)
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")

        try:
            deployment = self.apps_api.read_namespaced_deployment(
                name=self.faulty_service,
                namespace=self.namespace,
            )
        except ApiException as exc:
            if exc.status == 404:
                print(f"Deployment/{self.faulty_service} already absent; nothing to patch")
                return
            raise

        readiness_gates = deployment.spec.template.spec.readiness_gates or []
        remaining_gates = [gate for gate in readiness_gates if gate.condition_type != self.condition_type]

        if len(remaining_gates) == len(readiness_gates):
            print(f"Readiness gate '{self.condition_type}' already absent")
            return

        patch_body = {"spec": {"template": {"spec": {}}}}

        if remaining_gates:
            patch_body["spec"]["template"]["spec"]["readinessGates"] = [
                {"conditionType": gate.condition_type} for gate in remaining_gates
            ]
        else:
            patch_body["spec"]["template"]["spec"]["readinessGates"] = None

        self.apps_api.patch_namespaced_deployment(
            name=self.faulty_service,
            namespace=self.namespace,
            body=patch_body,
        )

        message = f"Removed orphan readiness gate '{self.condition_type}' from deployment/{self.faulty_service}."
        print(message)
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
