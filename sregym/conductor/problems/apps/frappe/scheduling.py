"""Frappe: scheduling."""

from __future__ import annotations

from sregym.conductor.oracles.assign_non_existent_node_mitigation import AssignNonExistentNodeMitigationOracle
from sregym.conductor.oracles.compound import CompoundedOracle
from sregym.conductor.oracles.incorrect_port import IncorrectPortAssignmentMitigationOracle
from sregym.conductor.problems.environment import configure_port
from sregym.conductor.problems.incorrect_port_assignment import IncorrectPortAssignment
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.utils.decorators import mark_fault_injected


class IncorrectPortAssignmentIA(IncorrectPortAssignment):
    """Frappe's nginx edge points at gunicorn's wrong port and is pinned to a missing node."""

    def __init__(
        self,
        app_name: str = "frappe",
        faulty_service: str = "erp-nginx",
        env_var: str = "BACKEND",
        correct_port: str = "8000",
        incorrect_port: str = "8001",
    ):
        self.faulty_service = faulty_service
        self.env_var = env_var
        self.correct_port = correct_port
        self.incorrect_port = incorrect_port
        self.unscheduable = True
        # The dependency is gunicorn's HTTP port, not Astronomy Shop's gRPC catalog.
        self.tcp_dependency_probe = True
        configure_port(
            self,
            app_name,
            component=f"deployment/{faulty_service}",
            description=(
                f"Two faults are active at the same time on deployment `{faulty_service}`: (1) its `{env_var}` "
                f"environment variable points to the wrong backend port ({incorrect_port} instead of "
                f"{correct_port}), so nginx cannot reach gunicorn, and (2) the deployment is pinned to a "
                "non-existent node via nodeSelector (`kubernetes.io/hostname: extra-node`), keeping its pods "
                "Pending. Symptoms are unschedulable pod events and, once scheduled, connection-refused errors "
                "from the bad upstream port."
            ),
            oracle_factory=lambda problem: CompoundedOracle(
                problem,
                IncorrectPortAssignmentMitigationOracle(problem=problem, require_source_ready=False),
                AssignNonExistentNodeMitigationOracle(problem=problem),
            ),
        )
        self.injector = ApplicationFaultInjector(namespace=self.namespace)
        self.injectors = {
            "incorrect_port_assignment": self.injector,
            "assign_to_non_existent_node": VirtualizationFaultInjector(namespace=self.namespace),
        }

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injectors["assign_to_non_existent_node"]._recover(
            fault_type="assign_to_non_existent_node", microservices=[self.faulty_service]
        )
        self.injector.recover_incorrect_port_assignment(
            deployment_name=self.faulty_service, env_var=self.env_var, correct_port=self.correct_port
        )
