import json
from typing import List

from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_hw import HWFaultInjector
from srearena.paths import TARGET_MICROSERVICES
from srearena.service.apps.hotel_reservation import HotelReservation
from srearena.service.kubectl import KubeCtl
from srearena.utils.decorators import mark_fault_injected


class ReadError(Problem):
    """
    Problem: inject syscall-level EIO (-5) failures into `read()` for the mongodb-user service.
    """

    def __init__(self):
        self.app = HotelReservation()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = "mongodb-user"  # matches the app= label or deployment name
        self.injector = HWFaultInjector()

        # (Optional) pick a request mix payload
        self.app.payload_script = (
            TARGET_MICROSERVICES / "hotelReservation/wrk2/scripts/hotel-reservation/mixed-workload_type_1.lua"
        )

        super().__init__(app=self.app, namespace=self.app.namespace)

        # Evaluate that the agent localizes the problem to the mongodb-user service
        self.localization_oracle = LocalizationOracle(problem=self, expected=[self.faulty_service])

        # Ensure the app’s workload generator is set up
        self.app.create_workload()

    # --------- Fault actions ----------

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection: read_error on mongodb-user ==")
        target_pods = self._pods_for_service(self.faulty_service)
        if not target_pods:
            raise RuntimeError(f"No pods found for service '{self.faulty_service}' in namespace '{self.namespace}'.")

        # Use Khaos HWFaultInjector (injects -EIO on read())
        self.injector.inject(target_pods, "read_error")
        print(f"Injected read_error into pods: {', '.join(target_pods)}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery: read_error ==")
        target_pods = self._pods_for_service(self.faulty_service)
        if not target_pods:
            # If the app was torn down already, just try to recover on all nodes touched earlier.
            # But in most runs we can still discover pods:
            print(f"[warn] No pods found for {self.faulty_service}; attempting best-effort recovery.")
            target_pods = []
        # Tell injector to call `khaos --recover read_error` on affected nodes
        self.injector.recover(target_pods, "read_error")
        print("Recovery request sent.\n")

    # --------- Helpers ----------

    def _pods_for_service(self, service_name: str) -> List[str]:
        """
        Returns pod references as 'namespace/pod' for mongodb-user.
        We try a few common label keys that HotelReservation uses.
        """
        ns = self.namespace

        # Try label selectors in order of likelihood
        selectors = [
            f"app={service_name}",
            f"service={service_name}",
            f"app.kubernetes.io/name={service_name}",
        ]

        pods: List[str] = []
        for sel in selectors:
            cmd = f"kubectl -n {ns} get pods -l {sel} -o json"
            out = self.kubectl.exec_command(cmd)
            if isinstance(out, tuple):
                out = out[0]
            try:
                data = json.loads(out)
            except Exception:
                continue

            for item in data.get("items", []):
                # Only running pods are useful for injection
                phase = item.get("status", {}).get("phase")
                if phase != "Running":
                    continue
                pods.append(f"{ns}/{item['metadata']['name']}")

            if pods:
                break  # Found some; stop at first matching selector

        # Fallback: list all pods in ns and filter by name substring
        if not pods:
            cmd = f"kubectl -n {ns} get pods -o json"
            out = self.kubectl.exec_command(cmd)
            if isinstance(out, tuple):
                out = out[0]
            try:
                data = json.loads(out)
                for item in data.get("items", []):
                    phase = item.get("status", {}).get("phase")
                    name = item["metadata"]["name"]
                    if phase == "Running" and service_name in name:
                        pods.append(f"{ns}/{name}")
            except Exception:
                pass

        return pods
