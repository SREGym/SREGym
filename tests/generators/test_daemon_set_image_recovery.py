import json
import stat
from types import SimpleNamespace

import pytest

from sregym.generators.fault import inject_virtual
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector


def _daemon_set(uid="daemon-set-uid", images=None):
    images = images or {"kube-proxy": "registry.k8s.io/kube-proxy:v1.31.14"}
    return SimpleNamespace(
        metadata=SimpleNamespace(uid=uid),
        spec=SimpleNamespace(
            template=SimpleNamespace(
                spec=SimpleNamespace(
                    containers=[SimpleNamespace(name=name, image=image) for name, image in images.items()]
                )
            )
        ),
    )


class _CoreApi:
    def __init__(self, cluster_uid="cluster-uid"):
        self.cluster_uid = cluster_uid

    def read_namespace(self, name):
        assert name == "kube-system"
        return SimpleNamespace(metadata=SimpleNamespace(uid=self.cluster_uid))


class _AppsApi:
    def __init__(self, daemon_set):
        self.daemon_set = daemon_set
        self.patches = []

    def read_namespaced_daemon_set(self, name, namespace):
        assert name == "kube-proxy"
        assert namespace == "kube-system"
        return self.daemon_set

    def patch_namespaced_daemon_set(self, name, namespace, body):
        assert name == "kube-proxy"
        assert namespace == "kube-system"
        self.patches.append(body)
        images = {container["name"]: container["image"] for container in body["spec"]["template"]["spec"]["containers"]}
        for container in self.daemon_set.spec.template.spec.containers:
            if container.name in images:
                container.image = images[container.name]


class _KubeCtl:
    def __init__(self, daemon_set, cluster_uid="cluster-uid"):
        self.core_v1_api = _CoreApi(cluster_uid)
        self.apps_v1_api = _AppsApi(daemon_set)
        self.commands = []
        self.rollout_error = None

    def exec_command_checked(self, command, timeout=None):
        self.commands.append((command, timeout))
        if self.rollout_error:
            raise self.rollout_error
        return ""


@pytest.fixture
def injector(monkeypatch, tmp_path):
    monkeypatch.setattr(inject_virtual, "DAEMON_SET_RECOVERY_STATE_DIR", tmp_path)
    instance = object.__new__(VirtualizationFaultInjector)
    instance.namespace = "kube-system"
    instance.kubectl = _KubeCtl(_daemon_set())
    return instance


def _state_path(injector):
    return injector._daemon_set_recovery_path("kube-proxy", injector._cluster_uid())


def test_injection_saves_the_real_images_before_replacement(injector):
    injector.inject_daemon_set_image_replacement("kube-proxy", "example.invalid/kube-proxy:broken")

    state = json.loads(_state_path(injector).read_text())
    assert state["cluster_uid"] == "cluster-uid"
    assert state["daemon_set_uid"] == "daemon-set-uid"
    assert state["images"] == {"kube-proxy": "registry.k8s.io/kube-proxy:v1.31.14"}
    assert injector._daemon_set_images(injector.kubectl.apps_v1_api.daemon_set) == {
        "kube-proxy": "example.invalid/kube-proxy:broken"
    }


def test_repeated_injection_keeps_the_first_snapshot(injector):
    injector.inject_daemon_set_image_replacement("kube-proxy", "example.invalid/kube-proxy:broken")
    injector.inject_daemon_set_image_replacement("kube-proxy", "example.invalid/kube-proxy:broken-again")

    state = json.loads(_state_path(injector).read_text())
    assert state["images"] == {"kube-proxy": "registry.k8s.io/kube-proxy:v1.31.14"}


def test_recovery_state_has_private_permissions(injector):
    injector.inject_daemon_set_image_replacement("kube-proxy", "example.invalid/kube-proxy:broken")

    path = _state_path(injector)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_failed_injection_patch_keeps_state_for_cleanup(injector):
    def fail_patch(*_args, **_kwargs):
        raise RuntimeError("patch failed")

    injector.kubectl.apps_v1_api.patch_namespaced_daemon_set = fail_patch

    with pytest.raises(RuntimeError, match="patch failed"):
        injector.inject_daemon_set_image_replacement("kube-proxy", "example.invalid/kube-proxy:broken")

    state = json.loads(_state_path(injector).read_text())
    assert state["images"] == {"kube-proxy": "registry.k8s.io/kube-proxy:v1.31.14"}


def test_recovery_restores_each_original_image_and_removes_state(injector):
    injector.kubectl.apps_v1_api.daemon_set = _daemon_set(
        images={
            "kube-proxy": "registry.k8s.io/kube-proxy:v1.31.14",
            "metrics": "example.invalid/metrics:v2",
        }
    )
    injector.inject_daemon_set_image_replacement("kube-proxy", "example.invalid/broken:v1")

    assert injector.recover_daemon_set_image_replacement("kube-proxy") is True
    assert injector._daemon_set_images(injector.kubectl.apps_v1_api.daemon_set) == {
        "kube-proxy": "registry.k8s.io/kube-proxy:v1.31.14",
        "metrics": "example.invalid/metrics:v2",
    }
    assert not _state_path(injector).exists()


def test_global_recovery_without_saved_state_does_not_change_the_daemon_set(injector):
    assert injector.recover_daemon_set_image_replacement("kube-proxy") is False
    assert injector.kubectl.apps_v1_api.patches == []
    assert injector.kubectl.commands == []


def test_recovery_rejects_a_recreated_daemon_set_and_keeps_state(injector):
    injector.inject_daemon_set_image_replacement("kube-proxy", "example.invalid/kube-proxy:broken")
    injector.kubectl.apps_v1_api.daemon_set.metadata.uid = "replacement-uid"

    with pytest.raises(RuntimeError, match="was recreated"):
        injector.recover_daemon_set_image_replacement("kube-proxy")

    assert _state_path(injector).exists()


def test_recovery_rejects_a_changed_container_set_and_keeps_state(injector):
    injector.inject_daemon_set_image_replacement("kube-proxy", "example.invalid/kube-proxy:broken")
    injector.kubectl.apps_v1_api.daemon_set.spec.template.spec.containers.append(
        SimpleNamespace(name="new-sidecar", image="example.invalid/sidecar:v1")
    )

    with pytest.raises(RuntimeError, match="containers do not match"):
        injector.recover_daemon_set_image_replacement("kube-proxy")

    assert _state_path(injector).exists()


def test_failed_recovery_rollout_keeps_state_for_the_next_run(injector):
    injector.inject_daemon_set_image_replacement("kube-proxy", "example.invalid/kube-proxy:broken")
    injector.kubectl.rollout_error = RuntimeError("rollout timed out")

    with pytest.raises(RuntimeError, match="rollout timed out"):
        injector.recover_daemon_set_image_replacement("kube-proxy")

    assert _state_path(injector).exists()


def test_recovery_state_from_another_cluster_is_not_used(monkeypatch, tmp_path):
    monkeypatch.setattr(inject_virtual, "DAEMON_SET_RECOVERY_STATE_DIR", tmp_path)
    old = object.__new__(VirtualizationFaultInjector)
    old.namespace = "kube-system"
    old.kubectl = _KubeCtl(_daemon_set(), cluster_uid="old-cluster")
    old.inject_daemon_set_image_replacement("kube-proxy", "example.invalid/kube-proxy:broken")

    current = object.__new__(VirtualizationFaultInjector)
    current.namespace = "kube-system"
    current.kubectl = _KubeCtl(_daemon_set(), cluster_uid="new-cluster")

    assert current.recover_daemon_set_image_replacement("kube-proxy") is False
    assert current.kubectl.apps_v1_api.patches == []
    assert old._daemon_set_recovery_path("kube-proxy", "old-cluster").exists()
