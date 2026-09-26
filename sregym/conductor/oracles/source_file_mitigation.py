from sregym.conductor.oracles.base import Oracle


class SourceFileMitigationOracle(Oracle):
    """Mitigation passes when the overlaid source file is no longer mounted
    from the fault ConfigMap and the target service's pods are Running."""

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        deployment_name = self.problem.faulty_service
        configmap_name = self.problem.configmap_name
        volume_name = f"{configmap_name}-vol"
        results = {}

        mount_gone = False
        try:
            deployment = kubectl.get_deployment(deployment_name, namespace)
            container = deployment.spec.template.spec.containers[0]
            volumes = deployment.spec.template.spec.volumes or []
            mounts = container.volume_mounts or []
            bad_volume = any(v.name == volume_name for v in volumes)
            bad_mount = any(m.name == volume_name for m in mounts)
            if not bad_volume and not bad_mount:
                print(
                    f"✅ Source overlay volume '{volume_name}' is no longer present on deployment '{deployment_name}'"
                )
                mount_gone = True
            else:
                print(
                    f"❌ Source overlay still present on deployment '{deployment_name}': "
                    f"volume={bad_volume}, mount={bad_mount}"
                )
        except Exception as e:
            print(f"❌ Failed to get deployment {deployment_name}: {e}")

        pods_ok = True
        if mount_gone:
            pod_list = kubectl.list_pods(namespace)
            for pod in pod_list.items:
                if pod.status.phase != "Running":
                    print(f"❌ Pod {pod.metadata.name} is in phase: {pod.status.phase}")
                    pods_ok = False
                    break
                for cs in pod.status.container_statuses or []:
                    if cs.state.waiting and cs.state.waiting.reason:
                        print(
                            f"❌ Container {cs.name} is waiting: {cs.state.waiting.reason}"
                        )
                        pods_ok = False
                    elif (
                        cs.state.terminated
                        and cs.state.terminated.reason != "Completed"
                    ):
                        print(
                            f"❌ Container {cs.name} terminated: {cs.state.terminated.reason}"
                        )
                        pods_ok = False
                if not pods_ok:
                    break

        success = mount_gone and pods_ok
        results["success"] = success
        print(f"Mitigation Result: {'✅ Pass' if success else '❌ Fail'}")
        return results
