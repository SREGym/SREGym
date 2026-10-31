import re

from sregym.conductor.oracles.mitigation import MitigationOracle


def _parse_config_threshold(value: str) -> float | None:
    """Parse a kubelet config line like 'nodefs.available: "85%"' into a float percentage."""
    if not value:
        return None
    m = re.search(r"([\d.]+)\s*%", value)
    return float(m.group(1)) if m else None


class KubeletEvictionThresholdMisconfigMitigationOracle(MitigationOracle):
    """Require a healthy application plus either a node repair or safe relocation."""

    def pods_unready(self, pods, **detail):
        def controller(pod):
            return next((owner for owner in (pod.metadata.owner_references or []) if owner.controller), None)

        # Node-local agents can remain evicted on a quarantined node. Require a
        # healthy peer of that same DaemonSet, not an unrelated Ready pod.
        healthy_daemonsets = {
            owner.uid
            for pod in pods
            if (owner := controller(pod)) is not None
            and owner.kind == "DaemonSet"
            and pod.spec.node_name != self.problem.target_node
            and pod.metadata.deletion_timestamp is None
            and pod.status.phase == "Running"
            and pod.status.container_statuses
            and all(container.ready for container in pod.status.container_statuses)
        }
        pressured = self._disk_pressure_active(self.problem.kubectl, self.problem.target_node)
        current = []
        for pod in pods:
            owner = controller(pod)
            history = pod.status.phase == "Succeeded" or (
                pod.status.phase == "Failed" and pod.status.reason == "Evicted"
            )
            # Current Deployment capacity is checked separately.
            if history and owner is not None and owner.kind == "ReplicaSet":
                continue
            if (
                owner is not None
                and owner.kind == "DaemonSet"
                and owner.uid in healthy_daemonsets
                and pod.spec.node_name == self.problem.target_node
                and (history or (pressured and pod.status.phase == "Pending"))
            ):
                continue
            current.append(pod)
        return super().pods_unready(current, **detail)

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

    def _placement_recovered(self, kubectl, namespace: str, faulty_service: str) -> bool:
        """Check the placement constraint after shared application-health checks."""
        deployments = kubectl.list_deployments(namespace)
        faulty = next((d for d in deployments.items if d.metadata.name == faulty_service), None)
        if faulty is None:
            print(f"❌ Deployment {faulty_service} not found in namespace {namespace}")
            return False
        pinned_node = faulty.spec.template.spec.node_name
        if pinned_node and self._disk_pressure_active(kubectl, pinned_node):
            print(f"❌ {faulty_service} still pinned to DiskPressure node {pinned_node}")
            return False
        print(f"✅ {faulty_service} no longer pinned to a DiskPressure node")
        return True

    def evaluate(self, solution=None, trace=None, duration=None) -> dict:
        print("== Kubelet Eviction Threshold Misconfig Mitigation Evaluation ==")

        result = super().evaluate()
        if not result.get("success"):
            return result

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

        if self._placement_recovered(kubectl, namespace, self.problem.faulty_service):
            return {"success": True}

        # Two independent acceptance paths both failed: the kubelet threshold is
        # still misconfigured with DiskPressure active, *and* the app did not
        # recover by rescheduling away from the node. Either alone could fail
        # environmentally; both failing together is the fault still in place.
        print("❌ Neither the node-level fix nor an app-level recovery was achieved")
        return self.fail("fault_still_present", node=target_node, namespace=namespace)
