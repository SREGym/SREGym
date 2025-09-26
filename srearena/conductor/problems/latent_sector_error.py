import json

from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_kernel import KernelInjector
from srearena.service.apps.hotel_reservation import HotelReservation
from srearena.service.kubectl import KubeCtl
from srearena.utils.decorators import mark_fault_injected


class LatentSectorError(Problem):
    """
    Simulates latent sector errors (LSE) on a MongoDB PVC
    (geo, profile, reservation, etc.) using dm-dust inside Khaos.
    """

    def __init__(self, target_deploy: str = "mongodb-geo", namespace: str = "hotel-reservation"):
        self.app = HotelReservation()
        self.kubectl = KubeCtl()
        self.namespace = namespace
        self.deploy = target_deploy
        self.injector = KernelInjector(self.kubectl)
        self.node = None
        self.pvc_path = None
        # TODO: We should throw an error if on an emulated cluster, this will only work on real linux nodes
        super().__init__(app=self.app, namespace=self.app.namespace)

    def _discover_node_for_deploy(self) -> str:
        """Return the node where the target deployment is running."""
        # First try with a label selector (common OpenEBS hotel-reservation pattern)
        svc = self.deploy.split("-", 1)[-1]  # e.g. "geo"
        cmd = f"kubectl -n {self.namespace} get pods -l app=mongodb,component={svc} -o json"
        out = self.kubectl.exec_command(cmd)
        if isinstance(out, tuple):
            out = out[0]
        data = json.loads(out or "{}")
        for item in data.get("items", []):
            if item.get("status", {}).get("phase") == "Running":
                return item["spec"]["nodeName"]

        # Fallback: search by pod name prefix
        cmd = f"kubectl -n {self.namespace} get pods -o json"
        out = self.kubectl.exec_command(cmd)
        if isinstance(out, tuple):
            out = out[0]
        data = json.loads(out or "{}")
        for item in data.get("items", []):
            name = item["metadata"]["name"]
            if name.startswith(self.deploy) and item.get("status", {}).get("phase") == "Running":
                return item["spec"]["nodeName"]

        return None

    def _discover_pvc(self) -> tuple[str, str, str]:
        """
        Return (pvc_name, pv_name, local_path)
        """
        cmd = f"kubectl -n {self.namespace} get pvc -o json"
        out = self.kubectl.exec_command(cmd)
        if isinstance(out, tuple):
            out = out[0]
        data = json.loads(out or "{}")

        pvc_name, pv_name = None, None
        for item in data.get("items", []):
            claim = item["metadata"]["name"]
            if self.deploy.split("-")[-1] in claim:  # match geo, profile, etc.
                pvc_name = claim
                pv_name = item["spec"]["volumeName"]
                break

        if not pvc_name:
            raise RuntimeError(f"Could not find PVC for deploy {self.deploy}")

        out = self.kubectl.exec_command(f"kubectl get pv {pv_name} -o json")
        if isinstance(out, tuple):
            out = out[0]
        pv = json.loads(out or "{}")
        local_path = pv["spec"]["local"]["path"]

        return pvc_name, pv_name, local_path

    @mark_fault_injected
    def inject_fault(self):
        print(f"[MongoDBLSE] Starting latent sector error injection for {self.deploy}")

        # Get target node where the deployment is running
        self.target_node = self._discover_node_for_deploy()
        if not self.target_node:
            raise RuntimeError(f"Could not find running node for deployment {self.deploy}")

        print(f"[MongoDBLSE] Target node: {self.target_node}")

        # Since dm-dust infrastructure is already set up by Conductor,
        # we just need to add bad blocks and enable them
        bad_blocks = [100, 200, 300]  # Add some bad blocks to simulate LSE

        print(f"[MongoDBLSE] Adding bad blocks: {bad_blocks}")
        self.injector.add_bad_blocks(self.target_node, bad_blocks)

        print(f"[MongoDBLSE] Enabling bad block simulation")
        self.injector.enable_bad_blocks(self.target_node)

        print(f"[MongoDBLSE] Latent sector error injection complete")

    @mark_fault_injected
    def recover_fault(self):
        print(f"[MongoDBLSE] Starting recovery from latent sector error injection")

        if hasattr(self, "target_node") and self.target_node:
            print(f"[MongoDBLSE] Disabling bad block simulation on {self.target_node}")
            self.injector.disable_bad_blocks(self.target_node)
        else:
            print(f"[MongoDBLSE] No target node found, skipping bad block disable")

        print(f"[MongoDBLSE] Recovery complete")
