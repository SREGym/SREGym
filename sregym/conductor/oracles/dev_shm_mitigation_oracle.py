from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class DevShmMitigationOracle(Oracle):
    """Hack-resistant mitigation oracle for the /dev/shm exhaustion problem.

    Passes only when:
    1. Worker deployment exists with >= 1 desired replica.
    2. Pod template mounts a memory-backed emptyDir (medium: Memory) at /dev/shm.
    3. All worker pods are Running and Ready.
    """

    importance = 1.0

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation (/dev/shm exhaustion) ==")
        apps_v1 = client.AppsV1Api()
        core_v1 = client.CoreV1Api()
        namespace = self.problem.namespace
        name = self.problem.worker_name

        try:
            deployment = apps_v1.read_namespaced_deployment(name, namespace)
        except ApiException as e:
            if e.status == 404:
                return {"success": False, "reason": f"Worker deployment '{name}' no longer exists."}
            raise
        desired = deployment.spec.replicas or 0
        if desired < 1:
            return {"success": False, "reason": f"Worker deployment '{name}' is scaled to {desired} replicas."}

        if not self._has_memory_backed_shm(deployment.spec.template.spec):
            return {
                "success": False,
                "reason": (
                    f"Worker '{name}' does not mount a memory-backed emptyDir (medium: Memory) at "
                    f"{self.problem.shm_mount_path}; the default 64 MiB shm is still in effect."
                ),
            }

        pods = core_v1.list_namespaced_pod(namespace, label_selector=f"app={name}").items
        if not pods:
            return {"success": False, "reason": f"No pods found for worker '{name}'."}
        for pod in pods:
            if pod.status.phase != "Running":
                return {"success": False, "reason": f"Pod {pod.metadata.name} is in phase {pod.status.phase}."}
            for cs in pod.status.container_statuses or []:
                if cs.state.waiting and cs.state.waiting.reason:
                    return {
                        "success": False,
                        "reason": f"Container {cs.name} is waiting: {cs.state.waiting.reason}.",
                    }
                if not cs.ready:
                    return {"success": False, "reason": f"Container {cs.name} is not ready."}

        return {"success": True}

    def _has_memory_backed_shm(self, pod_spec) -> bool:
        """Return True if a Memory-medium emptyDir is mounted at /dev/shm."""
        shm_volume_names = set()
        for container in pod_spec.containers or []:
            for mount in container.volume_mounts or []:
                if mount.mount_path == self.problem.shm_mount_path:
                    shm_volume_names.add(mount.name)
        if not shm_volume_names:
            return False
        for volume in pod_spec.volumes or []:
            if volume.name in shm_volume_names and volume.empty_dir and volume.empty_dir.medium == "Memory":
                return True
        return False
