from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_hw import HWFaultInjector
from srearena.paths import TARGET_MICROSERVICES
from srearena.service.apps.hotel_reservation import HotelReservation
from srearena.service.kubectl import KubeCtl
from srearena.utils.decorators import mark_fault_injected


class ReadError(Problem):
    """
    Problem: inject syscall-level EIO (-5) failures into `read()` for all pods on a target node.
    """

    def __init__(self, target_node: str = None):
        self.app = HotelReservation()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.injector = HWFaultInjector()
        self.target_node = target_node

        # (Optional) pick a request mix payload
        self.app.payload_script = (
            TARGET_MICROSERVICES / "hotelReservation/wrk2/scripts/hotel-reservation/mixed-workload_type_1.lua"
        )

        super().__init__(app=self.app, namespace=self.app.namespace)

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print(f"== Fault Injection: read_error ==")
        self.target_node = self.injector.inject_node(self.namespace, "read_error", self.target_node)
        print(f"[debug] target_node: {self.target_node}")
        # Setup localization oracle here since we now have the target node
        self.localization_oracle = LocalizationOracle(self, expected=[self.target_node])
        print(f"Injected read_error into pods on node {self.target_node}\n")

    @mark_fault_injected
    def recover_fault(self):
        print(f"== Fault Recovery: read_error on node {self.target_node} ==")
        if self.target_node:
            self.injector.recover_node(self.namespace, "read_error", self.target_node)
        else:
            print("[warn] No target node recorded; attempting best-effort recovery.")
        print("Recovery request sent.\n")
