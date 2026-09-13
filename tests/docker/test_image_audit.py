import json
import runpy
import shlex
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
with patch.object(sys, "path", [str(ROOT / "docker"), *sys.path]):
    AUDIT = runpy.run_path(str(ROOT / "docker/audit_images.py"))
IMAGES = json.loads((ROOT / "docker/images.lock.json").read_text())


def test_ansible_storage_and_cleanup_jobs_use_the_same_pinned_release():
    documents = list(yaml.safe_load_all((ROOT / "scripts/ansible/tidb/local-volume-provisioner.yaml").read_text()))
    daemonset = next(doc for doc in documents if doc["kind"] == "DaemonSet")
    container = daemonset["spec"]["template"]["spec"]["containers"][0]
    job_image = next(env["value"] for env in container["env"] if env["name"] == "JOB_CONTAINER_IMAGE")
    assert container["image"] == job_image == IMAGES["local-volume-provisioner"]
    assert "@sha256:" in job_image


def test_optional_ansible_controller_is_pinned_without_enabling_it():
    values = yaml.safe_load((ROOT / "scripts/ansible/tidb/tidb-operator.yaml").read_text())
    assert values["advancedStatefulset"]["image"] == IMAGES["advanced-statefulset"]
    assert values["advancedStatefulset"]["create"] is False


def test_source_extraction_covers_fault_constants_and_commands_not_annotations():
    source = '''
PINNED_CONCAT_IMAGE = ("ghcr.io/sregym/app:"
                      "v1")
PINNED_IMAGE = "ghcr.io/sregym/app:v2"
def run(image: str):
    pod(image="busybox:1.36")
    return {"image": "alpine:3.22"}
cmd = "kubectl run test --image=mysql:8"
manifest = """
containers:
  - image: redis:7
"""
'''
    assert AUDIT["source_images"](source, python=True) == {
        "ghcr.io/sregym/app:v2",
        "ghcr.io/sregym/app:v1",
        "busybox:1.36",
        "alpine:3.22",
        "mysql:8",
        "redis:7",
    }


def test_scanning_current_sources_includes_ansible_and_dynamic_helpers():
    inventory = AUDIT["Inventory"]()
    AUDIT["collect_sources"](inventory)
    assert "scripts/ansible/tidb/local-volume-provisioner.yaml" in inventory.sources[IMAGES["local-volume-provisioner"]]
    assert "scripts/ansible/tidb/tidb-operator.yaml" in inventory.sources[IMAGES["advanced-statefulset"]]
    assert "pingcap/tidb-backup-manager:v1.6.0" in inventory.sources
    assert "pingcap/tidb-backup-manager:v1.6.3" in inventory.sources
    assert "str" not in inventory.sources


def test_inventory_rejects_unresolved_templates_instead_of_silently_skipping():
    with pytest.raises(ValueError, match="Unresolved image"):
        AUDIT["Inventory"]().add(["example/app:{{ .Values.tag }}"], "new-chart")


def test_exceptions_do_not_hide_real_upstream_runtime_images():
    assert set(AUDIT["EXCLUDED"]) == {
        "app-image:latest",
        "pingcap/tidb-operatorr:v1.6.3",
        "sregym-agent-base:latest",
        "sregym:latest",
    }


def test_installer_audit_follows_generated_manifests_not_stale_outputs():
    script = """
dp_sample_yaml="deployment/yamls/deploy.yaml.sample"
dp_yaml="deployment/yamls/deploy.yaml"
cp $dp_sample_yaml $dp_yaml
kubectl apply -f deployment/yamls/deploy.yaml -n $namespace
# kubectl apply -f deployment/yamls/sw_deploy.yaml
kubectl apply -f deployment/monitoring
"""
    assert AUDIT["installer_manifests"](script) == {
        "deployment/yamls/deploy.yaml.sample",
        "deployment/monitoring",
    }


def test_unknown_installer_manifest_expressions_fail_closed():
    with pytest.raises(ValueError, match="Unresolved installer"):
        AUDIT["installer_manifests"]("kubectl apply -f $unknown")


@pytest.mark.parametrize("runtime", ["docker://27", "containerd://2"])
def test_chaos_install_passes_the_multiarch_pause_image_for_both_runtimes(runtime):
    from sregym.generators.noise.impl import stress_injector

    with (
        patch.object(stress_injector, "KubeCtl") as kubectl,
        patch.object(stress_injector, "Helm") as helm,
    ):
        kubectl.return_value.get_container_runtime.return_value = runtime
        kubectl.return_value.exec_command.return_value = ""
        helm.exists_release.return_value = False
        stress_injector.ChaosInjector("test")
    values = yaml.safe_load(Path(helm.install.call_args.kwargs["values_file"]).read_text())
    assert helm.install.call_args.kwargs["remote_chart"] is True
    assert values["controllerManager"]["podChaos"]["podFailure"]["pauseImage"] == IMAGES["chaos-pause"]


@pytest.mark.parametrize(
    ("runtime", "socket_path"),
    [
        ("docker", "/var/run/docker.sock"),
        ("containerd", "/run/containerd/containerd.sock"),
        ("crio", "/var/run/crio/crio.sock"),
    ],
)
def test_noise_install_uses_the_same_multiarch_overrides(runtime, socket_path):
    from sregym.generators.noise import manager

    def execute(command):
        if command.startswith("kubectl get nodes"):
            return f"node Ready {runtime}://1.0"
        if command.startswith("kubectl get pods"):
            return "controller 1/1 Running"
        return ""

    with patch.object(manager.NoiseManager, "_instance", None), patch.object(manager, "KubeCtl") as kubectl:
        kubectl.return_value.exec_command.side_effect = execute
        noise = manager.NoiseManager()
        noise._ensure_chaos_mesh_installed()

    commands = [call.args[0] for call in kubectl.return_value.exec_command.call_args_list]
    install_command = next(command for command in commands if command.startswith("helm upgrade --install"))
    args = shlex.split(install_command)
    values_file = Path(args[args.index("-f") + 1])
    assert values_file == ROOT / "sregym/generators/noise/impl/chaos-mesh-values.yaml"
    values = yaml.safe_load(values_file.read_text())
    assert values["controllerManager"]["podChaos"]["podFailure"]["pauseImage"] == IMAGES["chaos-pause"]
    assert f"chaosDaemon.runtime={runtime}" in args
    assert f"chaosDaemon.socketPath={socket_path}" in args
    assert args[args.index("--version") + 1] == "2.8.0"
    assert noise._chaos_mesh_ready is True
