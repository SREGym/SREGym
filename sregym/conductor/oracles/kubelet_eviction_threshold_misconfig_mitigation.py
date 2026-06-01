import re

from sregym.conductor.oracles.mitigation import MitigationOracle


def _parse_config_threshold(value: str) -> float | None:
    """Parse a kubelet config line like 'nodefs.available: "85%"' into a float percentage."""
    if not value:
        return None
    m = re.search(r"([\d.]+)\s*%", value)
    return float(m.group(1)) if m else None


class KubeletEvictionThresholdMisconfigMitigationOracle(MitigationOracle):
    """Pass when the kubelet eviction threshold is lowered/removed AND DiskPressure is cleared."""

    def _read_kubelet_config(self, injector, node_name: str) -> str:
        cmd = "grep 'nodefs.available' /var/lib/kubelet/config.yaml || true"
        if injector._check_is_kind():
            return injector._docker_exec(node_name, cmd)
        else:
            return injector._node_exec(node_name, cmd)

    def _disk_pressure_active(self, kubectl, node_name: str) -> bool | None:
        """Return True if DiskPressure=True, False if cleared, None if node not found."""
        node_list = kubectl.list_nodes()
        target = next((n for n in node_list.items if n.metadata.name == node_name), None)
        if target is None:
            return None
        for condition in target.status.conditions or []:
            if condition.type == "DiskPressure":
                return condition.status == "True"
        return False

    def _node_fix_applied(self, injector, kubectl, target_node: str) -> bool:
        """True when the kubelet threshold is corrected AND DiskPressure is cleared."""
        config_line = self._read_kubelet_config(injector, target_node).strip()

        if not config_line:
            print(f"✅ nodefs.available threshold removed from kubelet config on {target_node}")
        else:
            current = _parse_config_threshold(config_line)
            if current is None:
                print(f"❌ Could not parse threshold from kubelet config line: {config_line!r}")
                return False
            try:
                free_pct = kubectl.get_node_free_pct(target_node)
            except Exception as e:
                print(f"❌ Could not read kubelet stats summary for {target_node}: {e!r}")
                return False
            if current < free_pct:
                print(f"✅ Threshold below node free pct on {target_node}")
            else:
                print(
                    f"❌ Threshold still at or above node free pct on {target_node}: "
                    f"current={current}% free={free_pct}% (config: {config_line!r})"
                )
                return False

        active = self._disk_pressure_active(kubectl, target_node)
        if active is None:
            print(f"❌ Node {target_node} not found")
            return False
        if active:
            print(f"❌ Node {target_node} still has DiskPressure=True")
            return False

        print(f"✅ Node {target_node} DiskPressure cleared")
        return True

    def _app_recovered(self, kubectl, namespace: str, faulty_service: str) -> bool:
        """True when the faulty service no longer runs on a DiskPressure node AND every
        deployment has its desired ready replicas.
        """
        self._wait_for_rollouts(kubectl, namespace)
        deployments = kubectl.list_deployments(namespace)
        if not deployments.items:
            print(f"❌ No deployments found in namespace {namespace}")
            return False

        faulty = next((d for d in deployments.items if d.metadata.name == faulty_service), None)
        if faulty is not None:
            pinned_node = faulty.spec.template.spec.node_name
            if pinned_node and self._disk_pressure_active(kubectl, pinned_node):
                print(f"❌ {faulty_service} still pinned to DiskPressure node {pinned_node}")
                return False

        all_ready = True
        for dep in deployments.items:
            desired = dep.spec.replicas or 1
            ready = dep.status.ready_replicas or 0
            if ready < desired:
                print(f"❌ Deployment {dep.metadata.name}: {ready}/{desired} ready")
                all_ready = False

        if all_ready:
            print(f"✅ All deployments in {namespace} ready; {faulty_service} not on a DiskPressure node")
        return all_ready

    def evaluate(self, solution=None, trace=None, duration=None) -> dict:
        print("== Kubelet Eviction Threshold Misconfig Mitigation Evaluation ==")

        injector = self.problem.injector
        kubectl = self.problem.kubectl
        target_node = self.problem.target_node
        namespace = self.problem.namespace

        # Accept either mitigation path:
        #   1. Node-level fix: kubelet threshold corrected + DiskPressure cleared.
        #   2. App-level workaround: pods rescheduled to healthy nodes (e.g. pin removed),
        #      so the app is healthy even though the victim node stays misconfigured.
        if self._node_fix_applied(injector, kubectl, target_node):
            return {"success": True}

        if self._app_recovered(kubectl, namespace, self.problem.faulty_service):
            return {"success": True}

        return {"success": False}
