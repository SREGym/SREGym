from kubernetes import client

from sregym.conductor.oracles.alert_oracle import AlertOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.service_port_mismatch_oracle import ServicePortMismatchMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class ServicePortMismatch(Problem):
    APP_CONFIGS = {
        "hotel_reservation": {
            "app_factory": HotelReservation,
            "services": {
                "geo": {
                    "original_port": 8083,
                    "wrong_port": 18083,
                }
            },
        }
    }

    def __init__(self, app_name: str = "hotel_reservation", faulty_service: str = "geo"):
        self.app_name = app_name
        self.faulty_service = faulty_service

        app_config = self.APP_CONFIGS.get(app_name)
        if app_config is None:
            raise ValueError(f"Unsupported app name: {app_name}")

        service_config = app_config["services"].get(faulty_service)
        if service_config is None:
            raise ValueError(f"Unsupported service '{faulty_service}' for app '{app_name}'")

        self.app = app_config["app_factory"]()
        self.namespace = self.app.namespace
        self.original_port = service_config["original_port"]
        self.wrong_port = service_config["wrong_port"]

        super().__init__(app=self.app, namespace=self.namespace)

        self.kubectl = KubeCtl()
        self.core_v1 = client.CoreV1Api()
        self.root_cause = self.build_structured_root_cause(
            component=f"service/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"Service `{self.faulty_service}` exposes the wrong cluster-facing port ({self.wrong_port} instead of "
                f"{self.original_port}), so callers that still connect to `{self.faulty_service}:{self.original_port}` "
                "hit a service-layer connection failure even though the backing pods remain healthy and continue "
                "listening on the original container port."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.resolution_oracle = ServicePortMismatchMitigationOracle(problem=self)
        self.mitigation_oracle = AlertOracle(problem=self)

        self.app.create_workload()

    def _patch_service_port(self, port: int):
        patch = [
            {"op": "replace", "path": "/spec/ports/0/port", "value": port},
            {"op": "replace", "path": "/spec/ports/0/name", "value": str(port)},
        ]
        self.core_v1.patch_namespaced_service(
            name=self.faulty_service,
            namespace=self.namespace,
            body=patch,
            _content_type="application/json-patch+json",
        )

    @mark_fault_injected
    def inject_fault(self):
        self._patch_service_port(self.wrong_port)

    @mark_fault_injected
    def recover_fault(self):
        self._patch_service_port(self.original_port)
