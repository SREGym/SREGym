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

    def evaluate(self, solution=None, trace=None, duration=None) -> dict:
        print("== Kubelet Eviction Threshold Misconfig Mitigation Evaluation ==")

        injector = self.problem.injector
        kubectl = self.problem.kubectl
        target_node = self.problem.target_node

        # Check 1: kubelet config threshold must sit below the node's actual free-space ratio.
        config_line = self._read_kubelet_config(injector, target_node).strip()
        threshold_ok = False

        if not config_line:
            print(f"✅ nodefs.available threshold removed from kubelet config on {target_node}")
            threshold_ok = True
        else:
            current = _parse_config_threshold(config_line)
            if current is None:
                print(f"❌ Could not parse threshold from kubelet config line: {config_line!r}")
            else:
                try:
                    free_pct = injector._get_node_free_pct(target_node)
                except Exception as e:
                    print(f"❌ Could not read kubelet stats summary for {target_node}: {e!r}")
                    return {"success": False}

                if current < free_pct:
                    print(f"✅ Threshold below node free pct on {target_node}: ")
                    threshold_ok = True
                else:
                    print(
                        f"❌ Threshold still at or above node free pct on {target_node}: "
                        f"current={current}% free={free_pct}% (config: {config_line!r})"
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
