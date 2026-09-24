import copy
import json
import shlex
import tempfile
import time
from pathlib import Path

import yaml

from sregym.generators.fault.base import FaultInjector
from sregym.service.kubectl import KubeCtl

RECOVERY_STATE_DIR = Path(tempfile.gettempdir()) / "sregym-operator-faults"


class K8SOperatorFaultInjector(FaultInjector):
    def __init__(self, namespace: str):
        self.namespace = namespace
        self.kubectl = KubeCtl()
        self.kubectl.create_namespace_if_not_exist(namespace)

    def _state_path(self, fault: str) -> Path:
        # Namespace UIDs isolate clusters and fresh runs, including when recovery
        # constructs a new injector instance after the original one has exited.
        uid = str(self.kubectl.core_v1_api.read_namespace(self.namespace).metadata.uid or "")
        if not uid:
            raise RuntimeError("Cannot identify the namespace for TiDB recovery state")
        return RECOVERY_STATE_DIR / f"{uid}__{fault}.json"

    def _read_cluster(self, *, allow_missing: bool = False) -> dict | None:
        optional = " --ignore-not-found" if allow_missing else ""
        output = self.kubectl.exec_command_checked(
            f"kubectl get tidbcluster basic -n {shlex.quote(self.namespace)} -o json{optional}"
        )
        if not output.strip() and allow_missing:
            return None
        return json.loads(output)

    def _save_cluster(self, fault: str, cluster: dict) -> None:
        path = self._state_path(fault)
        uid = cluster["metadata"]["uid"]
        if path.exists() and json.loads(path.read_text())["uid"] == uid:
            return  # Repeated injection must not overwrite the clean snapshot.
        metadata = {
            key: copy.deepcopy(cluster["metadata"][key])
            for key in ("name", "namespace", "labels", "annotations")
            if key in cluster["metadata"]
        }
        metadata.get("annotations", {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
        original = {
            "apiVersion": cluster["apiVersion"],
            "kind": cluster["kind"],
            "metadata": metadata,
            "spec": copy.deepcopy(cluster["spec"]),
        }
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            json.dump({"uid": uid, "cluster": original}, stream)
            temporary_path = Path(stream.name)
        temporary_path.replace(path)

    def _load_cluster(self, fault: str) -> tuple[Path, dict]:
        path = self._state_path(fault)
        if not path.is_file():
            raise RuntimeError(f"Original TiDB configuration is missing for {fault}; refusing a generic replacement")
        return path, json.loads(path.read_text())

    def _inject_spec(self, fault: str, changes: dict) -> None:
        cluster = self._read_cluster()
        self._save_cluster(fault, cluster)
        patch = shlex.quote(json.dumps({"spec": changes}))
        self.kubectl.exec_command_checked(
            f"kubectl patch tidbcluster basic -n {shlex.quote(self.namespace)} --type=merge -p {patch}"
        )

    def recover_fault(self, fault: str) -> None:
        path, saved = self._load_cluster(fault)
        current = self._read_cluster()
        if current["metadata"]["uid"] != saved["uid"]:
            raise RuntimeError("TiDBCluster was recreated; refusing to restore stale recovery state")
        # JSON Patch removes fields introduced by the fault while retaining the
        # original images, versions, sizing and storage configuration exactly.
        patch = shlex.quote(json.dumps([{"op": "replace", "path": "/spec", "value": saved["cluster"]["spec"]}]))
        self.kubectl.exec_command_checked(
            f"kubectl patch tidbcluster basic -n {shlex.quote(self.namespace)} --type=json -p {patch}"
        )
        path.unlink()

    def inject_overload_replicas(self):
        self._inject_spec("overload-tidbcluster", {"tidb": {"replicas": 100000}})

    def recover_overload_replicas(self):
        self.recover_fault("overload-tidbcluster")

    def inject_invalid_affinity_toleration(self):
        self._inject_spec(
            "affinity-toleration-fault",
            {
                "tidb": {
                    "tolerations": [
                        {
                            "key": "test-keys",
                            "operator": "Equal",
                            "value": "test-value",
                            "effect": "TAKE_SOME_EFFECT",
                            "tolerationSeconds": 0,
                        }
                    ]
                }
            },
        )

    def recover_invalid_affinity_toleration(self):
        self.recover_fault("affinity-toleration-fault")

    def inject_security_context_fault(self):
        self._inject_spec("security-context-fault", {"tidb": {"podSecurityContext": {"runAsUser": -1}}})

    def recover_security_context_fault(self):
        self.recover_fault("security-context-fault")

    def inject_wrong_update_strategy(self):
        self._inject_spec(
            "deployment-update-strategy-fault", {"tidb": {"statefulSetUpdateStrategy": "SomeStrategyForUpdate"}}
        )

    def recover_wrong_update_strategy(self):
        self.recover_fault("deployment-update-strategy-fault")

    def inject_non_existent_storage(self):
        self._inject_spec("non-existent-storage-fault", {"pd": {"storageClassName": "nonexistent-storage-class"}})
        # StatefulSet volumeClaimTemplates are immutable. Recreate PD storage so
        # the invalid class actually leaves its replacement PVCs Pending.
        labels = "app.kubernetes.io/instance=basic,app.kubernetes.io/component=pd"
        namespace = shlex.quote(self.namespace)
        self.kubectl.exec_command_checked(f"kubectl delete pvc -n {namespace} -l {labels} --wait=false")
        self.kubectl.exec_command_checked(
            f"kubectl delete statefulset basic-pd -n {namespace} --ignore-not-found=true --wait=false"
        )

    def recover_non_existent_storage(self):
        path, saved = self._load_cluster("non-existent-storage-fault")
        current = self._read_cluster(allow_missing=True)
        if current is not None and current["metadata"]["uid"] != saved["uid"]:
            raise RuntimeError("TiDBCluster was recreated; refusing to delete a different cluster")
        namespace = shlex.quote(self.namespace)
        labels = "app.kubernetes.io/instance=basic,app.kubernetes.io/component=pd"
        # Finish removal before restoring the CR, otherwise PD can adopt a
        # leftover PVC carrying the invalid storage class.
        self.kubectl.exec_command_checked(
            f"kubectl delete tidbcluster basic -n {namespace} --ignore-not-found=true --cascade=foreground"
        )
        self.kubectl.exec_command_checked(f"kubectl delete pvc -n {namespace} -l {labels} --ignore-not-found=true")
        self.kubectl.exec_command_checked(f"kubectl apply -f - -n {namespace}", input_data=json.dumps(saved["cluster"]))
        path.unlink()

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


if __name__ == "__main__":
    namespace = "tidb-cluster"
    tidb_fault_injector = K8SOperatorFaultInjector(namespace)

    tidb_fault_injector.inject_wrong_operator_image()
    time.sleep(10)
    tidb_fault_injector.recover_wrong_operator_image()
