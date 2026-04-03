from sregym.conductor.oracles.assign_non_existent_node_mitigation import AssignNonExistentNodeMitigationOracle
from sregym.conductor.oracles.compound import CompoundedOracle
from sregym.conductor.oracles.incorrect_port import IncorrectPortAssignmentMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class IncorrectPortAssignment(Problem):
    def __init__(self, **kwargs):
        self.app = AstronomyShop()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)
        self.kubectl = KubeCtl()
        self.faulty_service = "checkout"
        self.env_var = "PRODUCT_CATALOG_ADDR"
        self.incorrect_port = "8082"
        self.correct_port = "8080"
        self.injector = ApplicationFaultInjector(namespace=self.namespace)
        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"The {self.env_var} environment variable points to the wrong backend port ({self.incorrect_port} instead of "
                f"{self.correct_port}), so checkout cannot reach product-catalog and related requests fail. "
                "Symptoms include connection-refused errors and elevated failed request rates on checkout endpoints."
            ),
        )

        if unscheduable := kwargs.get("unschedulable", False):
            self.unscheduable = unscheduable
            self.injectors = {
                "incorrect_port_assignment": self.injector,
                "assign_to_non_existent_node": VirtualizationFaultInjector(namespace=self.namespace),
            }
            self.root_cause = self.build_structured_root_cause(
                component=f"deployment/{self.faulty_service}",
                namespace=self.namespace,
                description=(
                    f"Two faults are active at the same time: (1) {self.env_var} points to the wrong backend port "
                    f"({self.incorrect_port} instead of {self.correct_port}), and (2) the deployment is pinned to a "
                    "non-existent node via nodeSelector, keeping pods Pending. Symptoms include both routing failures "
                    "from bad service endpoints and unschedulable pod events from node selector mismatch."
                ),
            )

        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = IncorrectPortAssignmentMitigationOracle(problem=self)

        if unscheduable := kwargs.get("unschedulable", False):
            mitigation_oracles = [
                IncorrectPortAssignmentMitigationOracle(problem=self),
                # for duplicated pvc mount, its just standard pod-status mitigation oracle.
                AssignNonExistentNodeMitigationOracle(problem=self),
            ]
            self.mitigation_oracle = CompoundedOracle(self, *mitigation_oracles)

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")

        if getattr(self, "unscheduable", False):
            self.injectors["assign_to_non_existent_node"]._inject(
                fault_type="assign_to_non_existent_node",
                microservices=[self.faulty_service],
            )
            print(
                f"Injected additional fault: duplicate PVC mounts for service {self.faulty_service} in namespace {self.namespace}\n"
            )

            self.injectors["incorrect_port_assignment"].inject_incorrect_port_assignment(
                deployment_name=self.faulty_service,
                component_label=self.faulty_service,
                env_var=self.env_var,
                incorrect_port=self.incorrect_port,
            )
        else:
            self.injector.inject_incorrect_port_assignment(
                deployment_name=self.faulty_service,
                component_label=self.faulty_service,
                env_var=self.env_var,
                incorrect_port=self.incorrect_port,
            )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        if getattr(self, "unscheduable", False):
            self.injectors["assign_to_non_existent_node"]._recover(
                fault_type="assign_to_non_existent_node",
                microservices=[self.faulty_service],
            )
            print(
                f"Recovered additional fault: duplicate PVC mounts for service {self.faulty_service} in namespace {self.namespace}\n"
            )
            self.injectors["incorrect_port_assignment"].recover_incorrect_port_assignment(
                deployment_name="checkout", env_var=self.env_var, correct_port="8080"
            )
        else:
            self.injector.recover_incorrect_port_assignment(
                deployment_name="checkout", env_var=self.env_var, correct_port="8080"
            )
