from sregym.conductor.oracles.incorrect_port import IncorrectPortAssignmentMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.compound import CompoundedOracle
from sregym.conductor.oracles.assign_non_existent_node_mitigation import AssignNonExistentNodeMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.assign_non_existent_node import AssignNonExistentNode
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
        self.root_cause = f"The deployment `{self.faulty_service}` has the environment variable `{self.env_var}` configured with an incorrect port `{self.incorrect_port}` instead of `{self.correct_port}`."

        if unscheduable := kwargs.get("unschedulable", False):
            self.unscheduable = unscheduable
            self.injectors = {
                "incorrect_port_assignment": self.injector,
                "assign_to_non_existent_node": VirtualizationFaultInjector(namespace=self.namespace)
            }
            self.root_cause = f"Two simultaneous faults: 1) The deployment `{self.faulty_service}` has the environment variable `{self.env_var}` configured with an incorrect port `{self.incorrect_port}` instead of `{self.correct_port}`." + f"The deployment `{self.faulty_service}` is configured with a nodeSelector pointing to a non-existent node (extra-node), causing pods to remain in Pending state."

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
            print(f"Injected additional fault: duplicate PVC mounts for service {self.faulty_service} in namespace {self.namespace}\n")

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
            print(f"Recovered additional fault: duplicate PVC mounts for service {self.faulty_service} in namespace {self.namespace}\n")
            self.injectors["incorrect_port_assignment"].recover_incorrect_port_assignment(
                deployment_name="checkout", env_var=self.env_var, correct_port="8080"
            )
        else:
            self.injector.recover_incorrect_port_assignment(
                deployment_name="checkout", env_var=self.env_var, correct_port="8080"
            )
