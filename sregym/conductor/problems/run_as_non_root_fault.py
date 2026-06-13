from kubernetes import client

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class RunAsNonRootFault(Problem):

    def __init__(self, faulty_service: str = "geo"):
        self.app = HotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = faulty_service

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"The container in deployment `{self.faulty_service}` has "
                "`securityContext.runAsNonRoot: true` applied, but the image defaults "
                "to UID 0 (root) because it contains no USER directive. Kubernetes "
                "rejects the container at admission — before it ever starts — with the "
                "error 'container has runAsNonRoot and image will run as root'. All "
                "replacement pods permanently land in CreateContainerConfigError and "
                "produce no application logs. Requests routed through this service fail."
            ),
        )

        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = MitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        apps_v1 = client.AppsV1Api()

        deployment = apps_v1.read_namespaced_deployment(
            name=self.faulty_service,
            namespace=self.namespace,
        )
        container_name = deployment.spec.template.spec.containers[0].name

        patch_body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": container_name,
                                "securityContext": {"runAsNonRoot": True},
                            }
                        ]
                    }
                }
            }
        }
        apps_v1.patch_namespaced_deployment(
            name=self.faulty_service,
            namespace=self.namespace,
            body=patch_body,
        )

        print(
            f"Patched deployment/{self.faulty_service}: "
            "containers[0].securityContext.runAsNonRoot=true"
        )
        print(
            "Expected symptom: new pods enter CreateContainerConfigError — "
            "container rejected at admission, no application logs produced."
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        apps_v1 = client.AppsV1Api()

        deployment = apps_v1.read_namespaced_deployment(
            name=self.faulty_service,
            namespace=self.namespace,
        )
        container_name = deployment.spec.template.spec.containers[0].name

        patch_body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": container_name,
                                "securityContext": None,
                            }
                        ]
                    }
                }
            }
        }
        apps_v1.patch_namespaced_deployment(
            name=self.faulty_service,
            namespace=self.namespace,
            body=patch_body,
        )

        print(
            f"Removed securityContext from deployment/{self.faulty_service} "
            "(runAsNonRoot constraint lifted)."
        )
        print("Expected: pods return to Running state once the constraint is removed.")
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
