from typing import Iterable
from kubernetes import client
from srearena.service.kubectl import KubeCtl


def wipe_openebs_volumes(
    storage_classes: Iterable[str] = ("openebs-hostpath", "openebs-device"),
    exclude_pvcs: Iterable[str] | None = None,
) -> None:
    exclude_pvcs = set(exclude_pvcs or [])
    kc = KubeCtl()
    v1 = kc.core_v1_api

    pvcs = v1.list_persistent_volume_claim_for_all_namespaces().items
    for pvc in pvcs:
        if pvc.metadata.name in exclude_pvcs:
            continue
        if pvc.spec.storage_class_name in storage_classes:
            try:
                v1.delete_namespaced_persistent_volume_claim(
                    name=pvc.metadata.name,
                    namespace=pvc.metadata.namespace,
                    body=client.V1DeleteOptions(propagation_policy="Foreground"),
                )
                print(
                    f"Deleted PVC {pvc.metadata.name} "
                    f"(SC={pvc.spec.storage_class_name}) in {pvc.metadata.namespace}"
                )
            except client.exceptions.ApiException as e:
                print(f"Failed to delete PVC {pvc.metadata.name}: {e}")


def wipe_prometheus_tsdb(namespace: str, label_selector: str = "app.kubernetes.io/name=prometheus") -> None:
    kc = KubeCtl()
    pods = kc.core_v1_api.list_namespaced_pod(namespace, label_selector=label_selector).items
    if not pods:
        print(f"⚠️  No Prometheus pods found in {namespace}; skipping TSDB wipe.")
        return

    for pod in pods:
        pod_name = pod.metadata.name
        kc.exec_command(f"kubectl exec -n {namespace} {pod_name} -- sh -c 'rm -rf /data/*'")
        kc.exec_command(f"kubectl delete pod -n {namespace} {pod_name}")
        print(f"🧹  Cleared TSDB and restarted pod {pod_name}")

    kc.wait_for_ready(namespace)
