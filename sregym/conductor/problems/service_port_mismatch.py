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
                    "original_service_port": 8083,
                    "original_target_port": 8083,
                    "wrong_target_port": 18083,
                    "deployment_name": "geo",
                    "pod_label_selector": "io.kompose.service=geo",
                    "config_path": "/go/src/github.com/harlow/go-micro-services/config.json",
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
        self.original_service_port = service_config["original_service_port"]
        self.original_target_port = service_config["original_target_port"]
        self.wrong_target_port = service_config["wrong_target_port"]
        self.deployment_name = service_config["deployment_name"]
        self.pod_label_selector = service_config["pod_label_selector"]
        self.config_path = service_config["config_path"]

        super().__init__(app=self.app, namespace=self.namespace)

        self.kubectl = KubeCtl()
        self.core_v1 = client.CoreV1Api()
        self.root_cause = self.build_structured_root_cause(
            component=f"service/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"Service `{self.faulty_service}` forwards traffic to the wrong backend port "
                f"(`targetPort {self.wrong_target_port}` instead of `{self.original_target_port}`). "
                f"Clients still call `{self.faulty_service}:{self.original_service_port}`, but the Service now maps "
                "those requests to a port where the healthy geo pods are not listening, creating a service-to-pod "
                "port mismatch."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.resolution_oracle = ServicePortMismatchMitigationOracle(problem=self)
        self.mitigation_oracle = AlertOracle(problem=self)

        self.app.create_workload()

    def _patch_service_target_port(self, target_port: int):
        patch = [
            {"op": "replace", "path": "/spec/ports/0/port", "value": self.original_service_port},
            {"op": "replace", "path": "/spec/ports/0/name", "value": str(self.original_service_port)},
            {"op": "replace", "path": "/spec/ports/0/targetPort", "value": target_port},
        ]
        self.core_v1.patch_namespaced_service(
            name=self.faulty_service,
            namespace=self.namespace,
            body=patch,
        )

    @mark_fault_injected
    def inject_fault(self):
        self._patch_service_target_port(self.wrong_target_port)

    @mark_fault_injected
    def recover_fault(self):
        self._patch_service_target_port(self.original_target_port)
