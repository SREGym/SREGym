from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.paths import TARGET_MICROSERVICES
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class TopOfRackRouterPartitionHotelReservation(Problem):
    def __init__(self, app_name: str = "hotel_reservation", faulty_service: str = "network-connectivity"):
        if app_name == "hotel_reservation":
            self.app = HotelReservation()
        else:
            raise ValueError(f"Unsupported app_name: {app_name}")
        self.app = HotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = faulty_service
        self.fault_type = "tor_network_partition"
        self.root_cause = self.build_structured_root_cause(
            component=f"network-group/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                "A top-of-rack switch failure isolates the selected node group from the rest of the cluster network, "
                "breaking east-west connectivity for affected microservices and causing cross-service timeouts. "
                "Symptoms include partial reachability where some services remain healthy while calls crossing "
                "the partition repeatedly fail or hang."
            ),
        )

        self.app.payload_script = (
            TARGET_MICROSERVICES / "hotelReservation/wrk2/scripts/hotel-reservation/mixed-workload_type_1.lua"
        )

        self.mitigation_oracle = MitigationOracle(problem=self)

        self.faulty_microservices: list[str] = []

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")

        deps = self.kubectl.exec_command(
            f"kubectl get deploy -n {self.namespace} -o jsonpath='{{.items[*].metadata.name}}'"
        ).split()
        self.faulty_microservices = [d for d in deps if self.faulty_service in d]

        if not self.faulty_microservices:
            raise RuntimeError(
                f"No deployments matched `{self.faulty_service}` in namespace `{self.namespace}`; "
                "cannot determine faulty group for ToR partition."
            )

        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._inject(
            fault_type=self.fault_type,
            microservices=self.faulty_microservices,
        )
        print(f"Services: {self.faulty_microservices} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")

        if not self.faulty_microservices:
            deps = self.kubectl.exec_command(
                f"kubectl get deploy -n {self.namespace} -o jsonpath='{{.items[*].metadata.name}}'"
            ).split()
            self.faulty_microservices = [d for d in deps if self.faulty_service in d]

        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._recover(fault_type=self.fault_type, microservices=self.faulty_microservices or ["_unused_"])
        print(f"Services: {self.faulty_microservices} | Namespace: {self.namespace}\n")
