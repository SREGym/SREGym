from types import SimpleNamespace
from unittest.mock import Mock

from kubernetes import client

from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.generators.fault.source_overlay import (
    apply_source_file_overlay,
    overlay_basename,
    override_configmap_name,
    override_volume_name,
    remove_source_file_overlay,
    select_container,
)


def _pod_spec(*container_names: str):
    containers = [client.V1Container(name=name, image="demo:latest") for name in container_names]
    return client.V1PodSpec(containers=containers)


def test_override_names_and_basename():
    assert override_configmap_name("recommendation") == "recommendation-src-override"
    assert override_configmap_name("recommendation", "custom-cm") == "custom-cm"
    assert override_volume_name("recommendation-src-override") == "recommendation-src-override-vol"
    assert overlay_basename("/app/recommendation_server.py") == "recommendation_server.py"


def test_apply_adds_configmap_subpath_mount_on_first_container():
    pod_spec = _pod_spec("recommendation", "istio-proxy")

    apply_source_file_overlay(
        pod_spec,
        volume_name="recommendation-src-override-vol",
        configmap_name="recommendation-src-override",
        source_path="/app/recommendation_server.py",
        basename="recommendation_server.py",
    )

    assert [volume.name for volume in pod_spec.volumes] == [
        "recommendation-src-override-vol",
        "recommendation-src-override-vol-pycache",
    ]
    volume = pod_spec.volumes[0]
    assert volume.config_map.name == "recommendation-src-override"
    assert pod_spec.volumes[1].empty_dir is not None

    rec = pod_spec.containers[0]
    assert [mount.name for mount in rec.volume_mounts] == [
        "recommendation-src-override-vol",
        "recommendation-src-override-vol-pycache",
    ]
    mount = rec.volume_mounts[0]
    assert mount.mount_path == "/app/recommendation_server.py"
    assert mount.sub_path == "recommendation_server.py"
    assert mount.read_only is True
    assert rec.volume_mounts[1].mount_path == "/app/__pycache__"
    assert pod_spec.containers[1].volume_mounts in (None, [])


def test_apply_is_idempotent_and_can_target_named_container():
    pod_spec = _pod_spec("istio-proxy", "recommendation")
    kwargs = dict(
        volume_name="src-vol",
        configmap_name="src-cm",
        source_path="/app/recommendation_server.py",
        basename="recommendation_server.py",
        container_name="recommendation",
    )

    apply_source_file_overlay(pod_spec, **kwargs)
    apply_source_file_overlay(pod_spec, **kwargs)

    assert [volume.name for volume in pod_spec.volumes] == ["src-vol", "src-vol-pycache"]
    rec = select_container(pod_spec, "recommendation")
    assert [mount.name for mount in rec.volume_mounts] == ["src-vol", "src-vol-pycache"]
    assert pod_spec.containers[0].volume_mounts in (None, [])


def test_remove_drops_volume_and_mount_and_leaves_other_volumes():
    pod_spec = _pod_spec("recommendation")
    pod_spec.volumes = [
        client.V1Volume(name="keep-me", empty_dir=client.V1EmptyDirVolumeSource()),
        client.V1Volume(name="src-vol", config_map=client.V1ConfigMapVolumeSource(name="src-cm")),
    ]
    pod_spec.containers[0].volume_mounts = [
        client.V1VolumeMount(name="keep-me", mount_path="/cache"),
        client.V1VolumeMount(
            name="src-vol",
            mount_path="/app/recommendation_server.py",
            sub_path="recommendation_server.py",
        ),
    ]

    remove_source_file_overlay(pod_spec, volume_name="src-vol")

    assert [volume.name for volume in pod_spec.volumes] == ["keep-me"]
    assert [mount.name for mount in pod_spec.containers[0].volume_mounts] == ["keep-me"]


def test_select_container_rejects_unknown_name():
    pod_spec = _pod_spec("recommendation")
    try:
        select_container(pod_spec, "missing")
    except RuntimeError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_inject_source_file_override_writes_configmap_and_rolls_out():
    pod_spec = _pod_spec("recommendation")
    deployment = SimpleNamespace(spec=SimpleNamespace(template=SimpleNamespace(spec=pod_spec)))
    injector = ApplicationFaultInjector.__new__(ApplicationFaultInjector)
    injector.namespace = "astronomy-shop"
    injector.kubectl = SimpleNamespace(
        create_or_update_configmap=Mock(),
        get_deployment=Mock(return_value=deployment),
        update_deployment=Mock(),
        exec_command_checked=Mock(return_value=""),
    )

    cm_name = injector.inject_source_file_override(
        "recommendation",
        "/app/recommendation_server.py",
        "print('buggy')\n",
    )

    assert cm_name == "recommendation-src-override"
    injector.kubectl.create_or_update_configmap.assert_called_once_with(
        "recommendation-src-override",
        "astronomy-shop",
        {"recommendation_server.py": "print('buggy')\n"},
    )
    injector.kubectl.update_deployment.assert_called_once_with("recommendation", "astronomy-shop", deployment)
    restarted = [call.args[0] for call in injector.kubectl.exec_command_checked.call_args_list]
    assert any("rollout restart deployment/recommendation" in cmd for cmd in restarted)
    assert any("rollout status deployment/recommendation" in cmd for cmd in restarted)
    mount = pod_spec.containers[0].volume_mounts[0]
    assert mount.sub_path == "recommendation_server.py"
    assert mount.mount_path == "/app/recommendation_server.py"


def test_recover_source_file_override_unmounts_and_deletes_configmap():
    pod_spec = _pod_spec("recommendation")
    apply_source_file_overlay(
        pod_spec,
        volume_name="recommendation-src-override-vol",
        configmap_name="recommendation-src-override",
        source_path="/app/recommendation_server.py",
        basename="recommendation_server.py",
    )
    deployment = SimpleNamespace(spec=SimpleNamespace(template=SimpleNamespace(spec=pod_spec)))
    injector = ApplicationFaultInjector.__new__(ApplicationFaultInjector)
    injector.namespace = "astronomy-shop"
    injector.kubectl = SimpleNamespace(
        get_deployment=Mock(return_value=deployment),
        update_deployment=Mock(),
        exec_command=Mock(return_value=""),
        exec_command_checked=Mock(return_value=""),
    )

    injector.recover_source_file_override("recommendation", "/app/recommendation_server.py")

    assert pod_spec.volumes == []
    assert pod_spec.containers[0].volume_mounts == []
    deleted = injector.kubectl.exec_command.call_args.args[0]
    assert "delete configmap recommendation-src-override" in deleted
    assert any(
        "rollout status deployment/recommendation" in call.args[0]
        for call in injector.kubectl.exec_command_checked.call_args_list
    )
