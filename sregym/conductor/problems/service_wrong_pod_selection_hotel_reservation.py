import json

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.wrong_pod_selection_mitigation import WrongPodSelectionMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class ServiceWrongPodSelectionHotelReservation(Problem):
    def __init__(self):
        self.app = HotelReservation()
        self.namespace = self.app.namespace
        self.frontend_service = "frontend"
        self.wrong_deployment = "search"
        self.route_label_key = "sregym.io/frontend-route"
        self.route_label_value = "true"
        self.expected_service_selector = {"io.kompose.service": "frontend"}
        self.faulty_service_selector = {self.route_label_key: self.route_label_value}
        self.expected_endpoint_pod_label = "frontend"

        super().__init__(app=self.app, namespace=self.namespace)
        self.kubectl = KubeCtl()
        self.root_cause = self.build_structured_root_cause(
            component=f"service/{self.frontend_service}",
            namespace=self.namespace,
            description=(
                "The `frontend` Service selector has been broadened to `sregym.io/frontend-route=true`, "
                "and that label is present on both the intended frontend pods and the unintended `search` pods. "
                "The frontend Service still has endpoints, but the endpoint list is polluted with a search pod. "
                "The search container listens on port 8082, not the frontend targetPort 5000, so traffic routed "
                "through the frontend Service can intermittently hit a pod that cannot serve frontend requests."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = WrongPodSelectionMitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        for deployment in [self.frontend_service, self.wrong_deployment]:
            self._set_pod_template_route_label(deployment, self.route_label_value)
            self._wait_for_rollout(deployment)

        self._replace_service_selector(self.faulty_service_selector)

        print(
            f"Service: {self.frontend_service} | Namespace: {self.namespace} | "
            f"Wrong endpoint deployment: {self.wrong_deployment}\n"
        )

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self._replace_service_selector(self.expected_service_selector)

        for deployment in [self.frontend_service, self.wrong_deployment]:
            self._set_pod_template_route_label(deployment, None)
            self._wait_for_rollout(deployment)

        print(f"Recovered frontend Service endpoint selection in namespace: {self.namespace}\n")

    def _set_pod_template_route_label(self, deployment: str, value: str | None):
        self.kubectl.patch_deployment(
            deployment,
            self.namespace,
            {"spec": {"template": {"metadata": {"labels": {self.route_label_key: value}}}}},
        )

    def _wait_for_rollout(self, deployment: str):
        self.kubectl.exec_command(f"kubectl rollout status deployment/{deployment} -n {self.namespace} --timeout=120s")

    def _replace_service_selector(self, selector: dict[str, str]):
        patch = json.dumps([{"op": "replace", "path": "/spec/selector", "value": selector}])
        self.kubectl.exec_command(
            f"kubectl patch svc {self.frontend_service} -n {self.namespace} --type=json -p='{patch}'"
        )
