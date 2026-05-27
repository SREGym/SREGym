import re

from sregym.conductor.oracles.mitigation import MitigationOracle


def _parse_threshold(value: str) -> float | None:
    """Parse a kubelet eviction threshold like '"95%"' or '85%' into a float percentage."""
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

    def evaluate(self, solution=None, trace=None, duration=None) -> dict:
        print("== Kubelet Eviction Threshold Misconfig Mitigation Evaluation ==")

        injector = self.problem.injector
        kubectl = self.problem.kubectl
        target_node = self.problem.target_node
        injected = _parse_threshold(self.problem.injected_threshold or "")

        # Check 1: kubelet config threshold
        config_line = self._read_kubelet_config(injector, target_node).strip()
        threshold_ok = False

        if not config_line:
            print(f"✅ nodefs.available threshold removed from kubelet config on {target_node}")
            threshold_ok = True
        else:
            current = _parse_threshold(config_line)
            if current is None:
                print(f"❌ Could not parse threshold from kubelet config line: {config_line!r}")
            elif injected is None:
                print(f"⚠️ No injected threshold recorded; cannot compare. Current: {current}%")
            elif current < injected:
                print(
                    f"✅ Threshold lowered on {target_node}: {current}% < injected {injected}% "
                    f"(config: {config_line!r})"
                )
                threshold_ok = True
            else:
                print(
                    f"❌ Threshold still at or above injected value on {target_node}: "
                    f"current={current}% injected={injected}% (config: {config_line!r})"
                )

        if not threshold_ok:
            return {"success": False}

        # Check 2: DiskPressure node condition cleared
        active = self._disk_pressure_active(kubectl, target_node)
        if active is None:
            print(f"❌ Node {target_node} not found")
            return {"success": False}
        if active:
            print(f"❌ Node {target_node} still has DiskPressure=True")
            return {"success": False}

        print(f"✅ Node {target_node} DiskPressure cleared")
        return {"success": True}
