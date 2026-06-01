"""Secret key reference mismatch causes CreateContainerConfigError."""

import copy
import time

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class SecretWrongKey(Problem):
    SECRET_NAME = "app-credentials"
    SECRET_REAL_KEY = "api-key"
    SECRET_WRONG_KEY = "api-token"

    def __init__(self, app_name: str = "hotel_reservation", faulty_service: str = "recommendation"):
        self.app_name = app_name
        self.faulty_service = faulty_service
        self.app = HotelReservation()
        self.namespace = self.app.namespace

        super().__init__(app=self.app, namespace=self.namespace)
        self.kubectl = KubeCtl()

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"deployment/{self.faulty_service} references key '{self.SECRET_WRONG_KEY}' "
                f"in Secret '{self.SECRET_NAME}', but the Secret only contains key "
                f"'{self.SECRET_REAL_KEY}'. Kubernetes cannot populate the environment "
                "variable and refuses to start the container. The pod stays in "
                "CreateContainerConfigError permanently. "
                "Fix by correcting the secretKeyRef key name in the deployment to match "
                f"the actual key '{self.SECRET_REAL_KEY}' present in the Secret."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.app.create_workload()
        self.mitigation_oracle = MitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)

        # Create the Secret with the REAL key
        secret_manifest = (
            f'{{"apiVersion":"v1","kind":"Secret","metadata":{{"name":"{self.SECRET_NAME}",'
            f'"namespace":"{self.namespace}"}},"stringData":'
            f'{{"{self.SECRET_REAL_KEY}":"dummy-credential-value"}}}}'
        )
        self.kubectl.exec_command(f"kubectl apply -f - <<'MANIFEST'\n{secret_manifest}\nMANIFEST")

        # Save original deployment for recovery
        original_yaml = injector._get_deployment_yaml(self.faulty_service)
        injector._write_yaml_to_file(self.faulty_service, original_yaml)

        # Patch deployment to reference the WRONG key
        faulty_yaml = copy.deepcopy(original_yaml)
        for container in faulty_yaml["spec"]["template"]["spec"]["containers"]:
            if "env" not in container:
                container["env"] = []
            container["env"].append(
                {
                    "name": "APP_API_KEY",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": self.SECRET_NAME,
                            "key": self.SECRET_WRONG_KEY,
                        }
                    },
                }
            )

        faulty_path = injector._write_yaml_to_file(self.faulty_service + "-faulty", faulty_yaml)

        self.kubectl.exec_command(f"kubectl delete deployment {self.faulty_service} -n {self.namespace}")
        self.kubectl.exec_command(f"kubectl apply -f {faulty_path} -n {self.namespace}")

        # Wait for fault to manifest — CreateContainerConfigError is not
        # handled by wait_for_stable so we use a fixed sleep instead
        time.sleep(30)

        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")

        # Restore original deployment (saved as /tmp/recommendation_modified.yaml)
        self.kubectl.exec_command(f"kubectl delete deployment {self.faulty_service} -n {self.namespace}")
        self.kubectl.exec_command(f"kubectl apply -f /tmp/{self.faulty_service}_modified.yaml -n {self.namespace}")
        # Clean up the injected Secret
        self.kubectl.exec_command(
            f"kubectl delete secret {self.SECRET_NAME} -n {self.namespace} --ignore-not-found=true"
        )
        self.kubectl.wait_for_ready(self.namespace)

        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
