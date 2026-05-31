from sregym.conductor.oracles.base import Oracle


class PDBBlocksDrainMitigationOracle(Oracle):
    importance = 1.0

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")
        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        results = {}

        all_normal = True

        # 1. No PDB in the namespace may block voluntary disruption.
        #    A correctly-fixed cluster has either no over-constrained PDB, or
        #    every PDB reports disruptionsAllowed > 0.
        pdb_json = kubectl.exec_command(
            f"kubectl get pdb -n {namespace} "
            '-o jsonpath=\'{range .items[*]}{.metadata.name}{"="}{.status.disruptionsAllowed}{"\\n"}{end}\''
        ).strip()

        if pdb_json:
            for line in pdb_json.splitlines():
                if "=" not in line:
                    continue
                name, allowed = line.rsplit("=", 1)
                allowed = allowed.strip()
                if allowed == "" or allowed == "0":
                    print(f"❌ PDB '{name}' still blocks disruption (disruptionsAllowed={allowed or 'unset'})")
                    all_normal = False

        # 2. No node may be left cordoned (SchedulingDisabled).
        nodes = kubectl.list_nodes()
        for node in nodes.items:
            if node.spec.unschedulable:
                print(f"❌ Node {node.metadata.name} is still cordoned (SchedulingDisabled)")
                all_normal = False

        # 3. All pods must be Running and ready.
        pod_list = kubectl.list_pods(namespace)
        if not pod_list.items:
            print("❌ No pods found in namespace")
            all_normal = False
        else:
            for pod in pod_list.items:
                if pod.status.phase != "Running":
                    print(f"❌ Pod {pod.metadata.name} is in phase: {pod.status.phase}")
                    all_normal = False
                    break
                if pod.status.container_statuses:
                    for cs in pod.status.container_statuses:
                        if not cs.ready:
                            print(f"⚠️ Container {cs.name} in {pod.metadata.name} is not ready")
                            all_normal = False
                if not all_normal:
                    break

        results["success"] = all_normal
        print(f"Mitigation Result: {'✅ Pass' if all_normal else '❌ Fail'}")
        return results
