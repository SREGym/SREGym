import json

from sregym.conductor.oracles.base import Oracle


class PodDisruptionBudgetMitigationOracle(Oracle):
    """Mitigation oracle that checks whether a PDB blocking evictions was fixed.

    Strategy:
    - Find PDBs in the problem namespace whose `minAvailable` is >= the deployment replicas
      and whose selector matches the deployment.
    - Treat the mitigation as successful only when no blocking PDB remains and the
      deployment has recovered.
    """

    def __init__(self, problem, deployment_name: str):
        super().__init__(problem)
        self.kubectl = problem.kubectl
        self.namespace = problem.namespace
        self.deployment_name = deployment_name

    @staticmethod
    def _selector_matches(deployment_selector: dict | None, pdb_selector: dict | None) -> bool:
        if not pdb_selector:
            return False

        pdb_labels = pdb_selector.get("matchLabels") or {}
        if not pdb_labels:
            return False

        deployment_labels = deployment_selector or {}
        return all(deployment_labels.get(key) == value for key, value in pdb_labels.items())

    def evaluate(self) -> dict:
        print("== PodDisruptionBudget Mitigation Evaluation ==")

        try:
            dep_json = self.kubectl.exec_command(
                f"kubectl get deployment {self.deployment_name} -n {self.namespace} -o json"
            )
            dep = json.loads(dep_json)
            replicas = dep.get("spec", {}).get("replicas", 1) or 1
            deployment_selector = dep.get("spec", {}).get("selector", {}).get("matchLabels") or {}

            pdbs_json = self.kubectl.exec_command(f"kubectl get pdb -n {self.namespace} -o json")
            pdbs = json.loads(pdbs_json).get("items", [])

            blocking_pdbs = []
            for pdb in pdbs:
                spec = pdb.get("spec", {})
                min_avail = spec.get("minAvailable")
                try:
                    min_avail_val = int(min_avail) if min_avail is not None else None
                except Exception:
                    min_avail_val = None

                if min_avail_val is None or min_avail_val < replicas:
                    continue

                if self._selector_matches(deployment_selector, spec.get("selector", {})):
                    blocking_pdbs.append(pdb)

            if blocking_pdbs:
                for pdb in blocking_pdbs:
                    pdb_name = pdb.get("metadata", {}).get("name")
                    min_available = pdb.get("spec", {}).get("minAvailable")
                    print(f"Blocking PDB still present: {pdb_name} (minAvailable={min_available})")
                return {"success": False}

            target_node = getattr(self.problem, "target_node", None)
            if target_node:
                node_json = self.kubectl.exec_command(f"kubectl get node {target_node} -o json")
                node = json.loads(node_json)
                if node.get("spec", {}).get("unschedulable", False):
                    print(f"Target node still cordoned: {target_node}")
                    return {"success": False}

            self.kubectl.wait_for_ready(self.namespace)

            check_json = self.kubectl.exec_command(
                f"kubectl get deployment {self.deployment_name} -n {self.namespace} -o json"
            )
            check = json.loads(check_json)
            avail = check.get("status", {}).get("availableReplicas", 0)
            desired = check.get("spec", {}).get("replicas", 1) or 1

            if avail >= desired:
                print(f"Mitigation successful: availableReplicas={avail} desired={desired}")
                return {"success": True}

            print(f"Mitigation incomplete: availableReplicas={avail} desired={desired}")
            return {"success": False}

        except Exception as e:
            print(f"Error during mitigation evaluation: {e}")
            return {"success": False}
