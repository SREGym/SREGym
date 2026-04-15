import time

import yaml

from sregym.generators.fault.base import FaultInjector
from sregym.service.kubectl import KubeCtl


class K8SOperatorFaultInjector(FaultInjector):
    def __init__(self, namespace: str):
        self.namespace = namespace
        self.kubectl = KubeCtl()
        self.kubectl.create_namespace_if_not_exist(namespace)

    def _apply_yaml(self, cr_name: str, cr_yaml: dict):
        yaml_path = f"/tmp/{cr_name}.yaml"
        with open(yaml_path, "w") as file:
            yaml.dump(cr_yaml, file)

        command = f"kubectl apply -f {yaml_path} -n {self.namespace}"
        print(f"Namespace: {self.namespace}")
        result = self.kubectl.exec_command(command)
        print(f"Injected {cr_name}: {result}")

    def _delete_yaml(self, cr_name: str):
        yaml_path = f"/tmp/{cr_name}.yaml"
        command = f"kubectl delete -f {yaml_path} -n {self.namespace}"
        result = self.kubectl.exec_command(command)
        print(f"Recovered from misconfiguration {cr_name}: {result}")

    def inject_overload_replicas(self):
        """
        Injects a TiDB misoperation custom resource.
        The misconfiguration sets an unreasonably high number of TiDB replicas.
        """
        cr_name = "overload-tidbcluster"
        cr_yaml = {
            "apiVersion": "pingcap.com/v1alpha1",
            "kind": "TidbCluster",
            "metadata": {"name": "basic", "namespace": self.namespace},
            "spec": {
                "version": "v3.0.8",
                "timezone": "UTC",
                "pvReclaimPolicy": "Delete",
                "pd": {
                    "baseImage": "pingcap/pd",
                    "replicas": 3,
                    "requests": {"storage": "1Gi"},
                    "config": {},
                },
                "tikv": {
                    "baseImage": "pingcap/tikv",
                    "replicas": 3,
                    "requests": {"storage": "1Gi"},
                    "config": {},
                },
                "tidb": {
                    "baseImage": "pingcap/tidb",
                    "replicas": 100000,  # Intentional misconfiguration
                    "service": {"type": "ClusterIP"},
                    "config": {},
                },
            },
        }

        self._apply_yaml(cr_name, cr_yaml)

    def recover_overload_replicas(self):
        self.recover_fault("overload-tidbcluster")

    def inject_invalid_affinity_toleration(self):
        """
        This misoperation specifies an invalid toleration effect.
        """
        cr_name = "affinity-toleration-fault"
        cr_yaml = {
            "apiVersion": "pingcap.com/v1alpha1",
            "kind": "TidbCluster",
            "metadata": {"name": "basic", "namespace": self.namespace},
            "spec": {
                "version": "v3.0.8",
                "timezone": "UTC",
                "pvReclaimPolicy": "Delete",
                "pd": {
                    "baseImage": "pingcap/pd",
                    "replicas": 3,
                    "requests": {"storage": "1Gi"},
                    "config": {},
                },
                "tikv": {
                    "baseImage": "pingcap/tikv",
                    "replicas": 3,
                    "requests": {"storage": "1Gi"},
                    "config": {},
                },
                "tidb": {
                    "baseImage": "pingcap/tidb",
                    "replicas": 2,
                    "service": {"type": "ClusterIP"},
                    "config": {},
                    "tolerations": [
                        {
                            "key": "test-keys",
                            "operator": "Equal",
                            "value": "test-value",
                            "effect": "TAKE_SOME_EFFECT",  # Buggy: invalid toleration effect
                            "tolerationSeconds": 0,
                        }
                    ],
                },
            },
        }
        self._apply_yaml(cr_name, cr_yaml)

    def recover_invalid_affinity_toleration(self):
        self.recover_fault("affinity-toleration-fault")

    def inject_security_context_fault(self):
        """
        The fault sets an invalid runAsUser value.
        """
        cr_name = "security-context-fault"
        cr_yaml = {
            "apiVersion": "pingcap.com/v1alpha1",
            "kind": "TidbCluster",
            "metadata": {"name": "basic", "namespace": self.namespace},
            "spec": {
                "version": "v3.0.8",
                "timezone": "UTC",
                "pvReclaimPolicy": "Delete",
                "pd": {
                    "baseImage": "pingcap/pd",
                    "replicas": 3,
                    "requests": {"storage": "1Gi"},
                    "config": {},
                },
                "tikv": {
                    "baseImage": "pingcap/tikv",
                    "replicas": 3,
                    "requests": {"storage": "1Gi"},
                    "config": {},
                },
                "tidb": {
                    "baseImage": "pingcap/tidb",
                    "replicas": 2,
                    "service": {"type": "ClusterIP"},
                    "config": {},
                    "podSecurityContext": {"runAsUser": -1},  # invalid runAsUser value
                },
            },
        }
        self._apply_yaml(cr_name, cr_yaml)

    def recover_security_context_fault(self):
        self.recover_fault("security-context-fault")

    def inject_wrong_update_strategy(self):
        """
        This fault specifies an invalid update strategy.
        """
        cr_name = "deployment-update-strategy-fault"
        cr_yaml = {
            "apiVersion": "pingcap.com/v1alpha1",
            "kind": "TidbCluster",
            "metadata": {"name": "basic", "namespace": self.namespace},
            "spec": {
                "version": "v3.0.8",
                "timezone": "UTC",
                "pvReclaimPolicy": "Delete",
                "pd": {
                    "baseImage": "pingcap/pd",
                    "replicas": 3,
                    "requests": {"storage": "1Gi"},
                    "config": {},
                },
                "tikv": {
                    "baseImage": "pingcap/tikv",
                    "replicas": 3,
                    "requests": {"storage": "1Gi"},
                    "config": {},
                },
                "tidb": {
                    "baseImage": "pingcap/tidb",
                    "replicas": 2,
                    "service": {"type": "ClusterIP"},
                    "config": {},
                    "statefulSetUpdateStrategy": "SomeStrategyForUpdate",  # invalid update strategy
                },
            },
        }
        self._apply_yaml(cr_name, cr_yaml)

    def recover_wrong_update_strategy(self):
        self.recover_fault("deployment-update-strategy-fault")

    def inject_non_existent_storage(self):
        """
        This fault specifies a non-existent storage class for PD.

        After updating the CR, deletes the PD StatefulSet and its PVCs so the
        TiDB operator recreates them using the bogus storageClass from the CR.
        New PVCs cannot be provisioned (StorageClass does not exist), so PD
        pods remain stuck in Pending — making the fault observable.
        """
        cr_name = "non-existent-storage-fault"
        cr_yaml = {
            "apiVersion": "pingcap.com/v1alpha1",
            "kind": "TidbCluster",
            "metadata": {"name": "basic", "namespace": self.namespace},
            "spec": {
                "version": "v3.0.8",
                "timezone": "UTC",
                "pvReclaimPolicy": "Delete",
                "pd": {
                    "baseImage": "pingcap/pd",
                    "replicas": 3,
                    "requests": {"storage": "1Gi"},
                    "config": {},
                    "storageClassName": "ThisIsAStorageClass",  # non-existent storage class
                },
                "tikv": {
                    "baseImage": "pingcap/tikv",
                    "replicas": 3,
                    "requests": {"storage": "1Gi"},
                    "config": {},
                },
                "tidb": {
                    "baseImage": "pingcap/tidb",
                    "replicas": 2,
                    "service": {"type": "ClusterIP"},
                    "config": {},
                },
            },
        }
        self._apply_yaml(cr_name, cr_yaml)

        # StatefulSet volumeClaimTemplates are immutable, so the operator
        # cannot propagate the updated storageClassName into the existing
        # StatefulSet or its already-bound PVCs.  Delete the PD StatefulSet
        # and its PVCs to force the operator to recreate them from the updated
        # CR.  The new PVCs will reference ThisIsAStorageClass, fail to
        # provision, and leave PD pods stuck in Pending.
        pd_labels = "app.kubernetes.io/instance=basic,app.kubernetes.io/component=pd"
        print("[FAULT] Deleting PD PVCs to force reprovisioning with the bogus storage class...")
        self.kubectl.exec_command(
            f"kubectl delete pvc -n {self.namespace} -l {pd_labels} --wait=false"
        )
        print("[FAULT] Deleting PD StatefulSet so the operator rebuilds it from the updated CR...")
        self.kubectl.exec_command(
            f"kubectl delete statefulset basic-pd -n {self.namespace} --ignore-not-found=true --wait=false"
        )

    def recover_non_existent_storage(self):
        # PVCs with the bogus storageClass are stuck in Pending and are not
        # automatically removed when the TidbCluster CR is deleted.  Delete
        # them first so the operator can provision fresh PVCs with the correct
        # storageClass after the CR is restored.
        pd_labels = "app.kubernetes.io/instance=basic,app.kubernetes.io/component=pd"
        print("[RECOVER] Deleting stuck PD PVCs (bogus storage class)...")
        self.kubectl.exec_command(
            f"kubectl delete pvc -n {self.namespace} -l {pd_labels} --ignore-not-found=true --wait=false"
        )
        self.recover_fault("non-existent-storage-fault")

    def inject_wrong_operator_image(self):
        """
        Fault: Replaces the operator pod image with a typo-version to trigger ImagePullBackOff.
        """
        # 1. Get the dynamic pod name and container name from the namespace
        # We use kubectl here because Pod names are not static like the 'basic' TidbCluster name
        pod_name = self.kubectl.exec_command(
            "kubectl get pods -n tidb-operator -o jsonpath='{.items[0].metadata.name}'"
        ).strip()
        container_name = self.kubectl.exec_command(
            f"kubectl get pod {pod_name} -n tidb-operator -o jsonpath='{{.spec.containers[0].name}}'"
        ).strip()

        # 2. Define the fault manifest as a python dict
        cr_name = "wrong-operator-image-fault"
        pod_yaml = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": pod_name, "namespace": "tidb-operator"},
            "spec": {
                "containers": [
                    {
                        "name": container_name,
                        "image": "pingcap/tidb-operatorr:v1.6.3",  # Typo in 'operatorr'
                    }
                ]
            },
        }

        # 3. Apply the fault
        yaml_path = f"/tmp/{cr_name}.yaml"
        with open(yaml_path, "w") as file:
            yaml.dump(pod_yaml, file)

        command = f"kubectl apply -f {yaml_path} -n tidb-operator"
        print(f"Namespace: {self.namespace}")
        result = self.kubectl.exec_command(command)
        print(f"Injected {cr_name}: {result}")

    def recover_wrong_operator_image(self):
        # 1. Get the dynamic pod name and container name from the namespace
        # We use kubectl here because Pod names are not static like the 'basic' TidbCluster name
        pod_name = self.kubectl.exec_command(
            "kubectl get pods -n tidb-operator -o jsonpath='{.items[0].metadata.name}'"
        ).strip()
        container_name = self.kubectl.exec_command(
            f"kubectl get pod {pod_name} -n tidb-operator -o jsonpath='{{.spec.containers[0].name}}'"
        ).strip()

        # 2. Define the fault manifest as a python dict
        cr_name = "recover-wrong-operator-image-fault"
        pod_yaml = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": pod_name, "namespace": "tidb-operator"},
            "spec": {"containers": [{"name": container_name, "image": "pingcap/tidb-operator:v1.6.3"}]},
        }

        # 3. Recover the fault
        yaml_path = f"/tmp/{cr_name}.yaml"
        with open(yaml_path, "w") as file:
            yaml.dump(pod_yaml, file)

        command = f"kubectl apply -f {yaml_path} -n tidb-operator"
        print(f"Namespace: {self.namespace}")
        result = self.kubectl.exec_command(command)
        print(f"Injected {cr_name}: {result}")

    def recover_fault(self, cr_name: str):
        self._delete_yaml(cr_name)
        clean_url = "https://raw.githubusercontent.com/pingcap/tidb-operator/v1.6.0/examples/basic/tidb-cluster.yaml"
        command = f"kubectl apply -f {clean_url} -n {self.namespace}"
        result = self.kubectl.exec_command(command)
        print(f"Restored clean TiDBCluster: {result}")


if __name__ == "__main__":
    namespace = "tidb-cluster"
    tidb_fault_injector = K8SOperatorFaultInjector(namespace)

    tidb_fault_injector.inject_wrong_operator_image()
    time.sleep(10)
    tidb_fault_injector.recover_wrong_operator_image()
