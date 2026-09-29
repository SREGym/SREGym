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

    # This oracle already returned a ``reason``, but as English sentences --
    # "Worker deployment 'x' is scaled to 0 replicas." Useful to read, useless
    # to filter or group on, and it interpolated names into the value so no two
    # runs shared a string. The sentences move to prints, which is where the
    # human-readable path belongs, and ``reason`` becomes a stable code with the
    # varying parts in ``detail``.
    #
    # Every reason here is shared, so there is no local table: the memory-backed
    # /dev/shm check is the fault signature and uses ``fault_still_present``.

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
                print(f"❌ Worker deployment '{name}' no longer exists.")
                return self.fail("required_deployment_missing", deployment=name, namespace=namespace)
            raise
        desired = deployment.spec.replicas or 0
        if desired < 1:
            print(f"❌ Worker deployment '{name}' is scaled to {desired} replicas.")
            return self.fail("required_deployment_scaled_to_zero", deployment=name, desired=desired)

        if not self._has_memory_backed_shm(deployment.spec.template.spec):
            print(
                f"❌ Worker '{name}' does not mount a memory-backed emptyDir (medium: Memory) at "
                f"{self.problem.shm_mount_path}; the default 64 MiB shm is still in effect."
            )
            return self.fail("fault_still_present", deployment=name, mount_path=self.problem.shm_mount_path)

        pods = core_v1.list_namespaced_pod(namespace, label_selector=f"app={name}").items
        if not pods:
            print(f"❌ No pods found for worker '{name}'.")
            return self.fail("no_pods_found", namespace=namespace, selector=f"app={name}")

        unready = self.pods_unready(pods, deployment=name)
        if unready is not None:
            return unready

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
