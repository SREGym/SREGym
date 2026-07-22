import logging
from sregym.conductor.oracles.base import Oracle

logger = logging.getLogger(__name__)

class ReadinessProbeMissingBinaryMitigationOracle(Oracle):
    """Mitigation oracle for the ReadinessProbeMissingBinary problem.

    This oracle verifies that the `recommendation` deployment's readiness probe
    no longer references the missing `/usr/local/bin/healthcheck` binary, and that
    all pods in the application namespace are Running and Ready.
    """

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation (Missing Binary Readiness Probe) ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        faulty_service = self.problem.faulty_service
        results = {}

        # 1. Fetch deployment details
        try:
            deployment = kubectl.apps_v1_api.read_namespaced_deployment(
                name=faulty_service, namespace=namespace
            )
        except Exception as e:
            print(f"❌ Failed to read deployment {faulty_service}: {e}")
            results["success"] = False
            return results

        containers = deployment.spec.template.spec.containers
        if not containers:
            print("❌ No containers found in deployment spec")
            results["success"] = False
            return results

        # 2. Check if the readiness probe has been fixed
        probe_ok = True
        for container in containers:
            probe = container.readiness_probe
            if not probe:
                # If they deleted the probe, the pod might become Ready.
                # However, they should ideally replace it with a valid probe.
                # But we accept deletion or correction as mitigation.
                continue

            if probe.exec and probe.exec.command:
                cmd = " ".join(probe.exec.command)
                if "/usr/local/bin/healthcheck" in cmd:
                    print(f"❌ Container {container.name} still uses the missing binary in its readiness probe: {cmd}")
                    probe_ok = False
                    break

        if not probe_ok:
            results["success"] = False
            return results

        # 3. Check if all pods in the namespace are Running and Ready
        pod_list = kubectl.list_pods(namespace)
        if not pod_list.items:
            print("❌ No pods found in namespace")
            results["success"] = False
            return results

        all_normal = True
        for pod in pod_list.items:
            if pod.status.phase != "Running":
                print(f"❌ Pod {pod.metadata.name} is in phase: {pod.status.phase}")
                all_normal = False
                break

            if pod.status.container_statuses:
                for container_status in pod.status.container_statuses:
                    if container_status.state.waiting and container_status.state.waiting.reason:
                        print(f"❌ Container {container_status.name} is waiting: {container_status.state.waiting.reason}")
                        all_normal = False
                    elif container_status.state.terminated and container_status.state.terminated.reason != "Completed":
                        print(f"❌ Container {container_status.name} terminated: {container_status.state.terminated.reason}")
                        all_normal = False
                    elif not container_status.ready:
                        print(f"⚠️ Container {container_status.name} is not ready")
                        all_normal = False

            if not all_normal:
                break

        results["success"] = all_normal
        print(f"Mitigation Result: {'Pass ✅' if all_normal else 'Fail ❌'}")
        return results
