from types import SimpleNamespace

import yaml
from kubernetes.client.rest import ApiException

from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector


def _baseline_deployment():
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "jaeger",
            "namespace": "social-network",
            "uid": "runtime-uid",
            "resourceVersion": "123",
            "generation": 4,
            "annotations": {
                "deployment.kubernetes.io/revision": "4",
                "helm.sh/release": "social-network",
            },
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "jaeger"}},
            "template": {
                "metadata": {"labels": {"app": "jaeger"}},
                "spec": {
                    "containers": [
                        {
                            "name": "jaeger",
                            "image": "jaegertracing/all-in-one:1.57",
                            "volumeMounts": [{"name": "config", "mountPath": "/etc/jaeger"}],
                        }
                    ],
                    "volumes": [{"name": "config", "configMap": {"name": "jaeger-config"}}],
                },
            },
        },
        "status": {"readyReplicas": 1},
    }


class _CoreV1:
    def __init__(self, claims=None):
        self.claims = set(claims or [])

    def read_namespaced_persistent_volume_claim(self, name, namespace):
        if name not in self.claims:
            raise ApiException(status=404)
        return SimpleNamespace(metadata=SimpleNamespace(name=name))

    def list_namespaced_persistent_volume_claim(self, namespace):
        return SimpleNamespace(
            items=[SimpleNamespace(metadata=SimpleNamespace(name=name)) for name in sorted(self.claims)]
        )


class _AppsV1:
    def __init__(self, statefulset=None):
        self.statefulset = statefulset

    def read_namespaced_stateful_set(self, name, namespace):
        if self.statefulset is None:
            raise ApiException(status=404)
        return self.statefulset


class _KubeCtl:
    def __init__(self, deployment_yaml, *, claims=None, statefulset=None):
        self.deployment_yaml = deployment_yaml
        self.core_v1_api = _CoreV1(claims)
        self.apps_v1_api = _AppsV1(statefulset)
        self.commands = []
        self.wait_calls = []
        self.deployment_reads = []

    def exec_command(self, command):
        self.commands.append(command)
        if command.startswith("kubectl get deployment"):
            return yaml.safe_dump(self.deployment_yaml)
        if command.startswith("kubectl apply -f -"):
            manifest = yaml.safe_load(command.split("<<EOF\n", 1)[1].rsplit("\nEOF", 1)[0])
            self.core_v1_api.claims.add(manifest["metadata"]["name"])
        if command.startswith("kubectl delete pvc "):
            claim_name = command.split()[3]
            self.core_v1_api.claims.discard(claim_name)
        return "ok"

    def get_deployment(self, name, namespace):
        self.deployment_reads.append((name, namespace))
        return SimpleNamespace(metadata=SimpleNamespace(name=name))

    def wait_for_ready(self, namespace, service_names, max_wait):
        self.wait_calls.append((namespace, service_names, max_wait))


def _injector(tmp_path, kubectl):
    injector = VirtualizationFaultInjector.__new__(VirtualizationFaultInjector)
    injector.namespace = "social-network"
    injector.kubectl = kubectl
    injector._storage_baseline_path = lambda service: tmp_path / f"deployment-state-{service}.yaml"
    return injector


def test_injection_saves_baseline_and_creates_the_expected_storage_conflict(tmp_path):
    baseline = _baseline_deployment()
    kubectl = _KubeCtl(baseline)
    injector = _injector(tmp_path, kubectl)
    written_manifests = []
    injector._write_yaml_to_file = lambda service, manifest: written_manifests.append(manifest) or "/tmp/jaeger.yaml"

    injector.inject_duplicate_pvc_mounts(["jaeger"])

    snapshot = yaml.safe_load((tmp_path / "deployment-state-jaeger.yaml").read_text())
    assert snapshot["kind"] == "Deployment"
    assert snapshot["spec"] == baseline["spec"]
    assert "status" not in snapshot
    assert "uid" not in snapshot["metadata"]
    assert snapshot["metadata"]["annotations"] == {"helm.sh/release": "social-network"}

    modified = written_manifests[0]
    assert modified["spec"]["replicas"] == 2
    assert modified["spec"]["template"]["spec"]["volumes"][-1] == {
        "name": "jaeger-volume",
        "persistentVolumeClaim": {"claimName": "jaeger-pvc"},
    }
    assert modified["spec"]["template"]["spec"]["containers"][0]["volumeMounts"][-1] == {
        "name": "jaeger-volume",
        "mountPath": "/jaeger-data",
    }
    anti_affinity = modified["spec"]["template"]["spec"]["affinity"]["podAntiAffinity"]
    assert anti_affinity["requiredDuringSchedulingIgnoredDuringExecution"][0]["topologyKey"] == (
        "kubernetes.io/hostname"
    )
    assert "jaeger-pvc" in kubectl.core_v1_api.claims
    assert baseline["spec"]["replicas"] == 1


def test_injection_refuses_to_modify_a_preexisting_claim(tmp_path):
    kubectl = _KubeCtl(_baseline_deployment(), claims={"jaeger-pvc"})
    injector = _injector(tmp_path, kubectl)

    try:
        injector.inject_duplicate_pvc_mounts(["jaeger"])
    except RuntimeError as exc:
        assert "Refusing to replace pre-existing PersistentVolumeClaim" in str(exc)
    else:
        raise AssertionError("Expected injection to reject the pre-existing claim")

    assert not (tmp_path / "deployment-state-jaeger.yaml").exists()
    assert not any(command.startswith("kubectl apply") for command in kubectl.commands)


def test_recovery_restores_deployment_and_removes_current_and_legacy_claims(tmp_path):
    baseline_path = tmp_path / "deployment-state-jaeger.yaml"
    baseline_path.write_text(yaml.safe_dump(_baseline_deployment()))
    statefulset = SimpleNamespace(
        spec=SimpleNamespace(volume_claim_templates=[SimpleNamespace(metadata=SimpleNamespace(name="data-volume"))])
    )
    kubectl = _KubeCtl(
        _baseline_deployment(),
        claims={"jaeger-pvc", "data-volume-jaeger-0", "unrelated-pvc"},
        statefulset=statefulset,
    )
    injector = _injector(tmp_path, kubectl)

    injector.recover_duplicate_pvc_mounts(["jaeger"])

    assert any(command.startswith("kubectl delete statefulset jaeger") for command in kubectl.commands)
    assert any(command.startswith("kubectl delete deployment jaeger") for command in kubectl.commands)
    assert any("kubectl delete pvc jaeger-pvc" in command for command in kubectl.commands)
    assert any("kubectl delete pvc data-volume-jaeger-0" in command for command in kubectl.commands)
    assert any(f"kubectl apply -f {baseline_path}" in command for command in kubectl.commands)
    assert any("rollout status deployment/jaeger" in command for command in kubectl.commands)
    assert kubectl.core_v1_api.claims == {"unrelated-pvc"}
    assert kubectl.deployment_reads == [("jaeger", "social-network")]
    assert kubectl.wait_calls == [("social-network", "jaeger", 180)]
    assert not baseline_path.exists()


def test_recovery_requires_the_saved_deployment_before_deleting_resources(tmp_path):
    kubectl = _KubeCtl(_baseline_deployment(), claims={"jaeger-pvc"})
    injector = _injector(tmp_path, kubectl)

    try:
        injector.recover_duplicate_pvc_mounts(["jaeger"])
    except RuntimeError as exc:
        assert "Saved Deployment configuration is missing" in str(exc)
    else:
        raise AssertionError("Expected recovery to require the saved Deployment")

    assert kubectl.commands == []
    assert kubectl.core_v1_api.claims == {"jaeger-pvc"}
