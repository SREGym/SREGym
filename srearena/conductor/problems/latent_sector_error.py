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
        # Find node where MongoDB is running
        node = self._discover_node_for_deploy()
        if not node:
            raise RuntimeError(f"Could not find a running pod for {self.deploy}")
        self.node = node
        print(f"[MongoDBLSE] Target node for injection: {node}")

        # Discover PVC metadata (name, PV, local path)
        pvc_name, pv_name, local_path = self._discover_pvc()
        self.pvc_name = pvc_name
        self.pv_name = pv_name
        self.pvc_path = local_path
        print(f"[MongoDBLSE] PVC {pvc_name} -> PV {pv_name} -> {local_path}")

        # Scale down deploy to release PVC
        self.kubectl.exec_command(f"kubectl -n {self.namespace} scale deploy/{self.deploy} --replicas=0")

        # Inject LSE on this node/PVC
        self.injector.inject_lse(node=node, pvc_name=pvc_name, namespace=self.app.namespace)

        # Scale back up
        self.kubectl.exec_command(f"kubectl -n {self.namespace} scale deploy/{self.deploy} --replicas=1")

    @mark_fault_injected
    def recover_fault(self):
        self.injector.recover_lse()
