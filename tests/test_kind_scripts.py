import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="KIND setup requires a Unix shell")
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def shell_environment(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "kind"
    scripts.mkdir(parents=True)
    (scripts / "setup_kind_cluster.sh").write_text((REPO_ROOT / "kind/setup_kind_cluster.sh").read_text())
    (scripts / "kind-config.yaml").touch()
    commands = tmp_path / "bin"
    commands.mkdir()
    for command in ("kind", "kubectl", "rm"):
        executable = commands / command
        executable.write_text(
            f'#!/bin/sh\ncommand_name={command}\nprintf "%s %s\\n" "$command_name" "$*" >> "$KIND_TEST_CALL_LOG"\n'
            'if [ "${KIND_TEST_FAIL:-}" = "$command_name" ]; then exit 1; fi\n'
            'if [ "$command_name" = kubectl ]; then\n'
            '  case "$1 $2" in\n'
            '    "get nodes")\n'
            '      if [ "${KIND_TEST_NO_PROBE_NODES:-}" != true ]; then printf "worker-a\\nworker-b\\n"; fi ;;\n'
            '    "run "*) [ "${KIND_TEST_FAIL_PROBE:-}" != run ] || exit 1 ;;\n'
            '    "wait --for=condition=Ready")\n'
            '      case "$3" in pod/sregym-cni-probe-*)\n'
            '        [ "${KIND_TEST_FAIL_PROBE:-}" != wait ] || exit 1\n'
            '        if [ "${KIND_TEST_FAIL_PROBE:-}" = interrupt ]; then kill -TERM "$PPID"; exit 143; fi ;;\n'
            "      esac ;;\n"
            '    "delete pod") [ "${KIND_TEST_FAIL_PROBE:-}" != delete ] || exit 1 ;;\n'
            "  esac\n"
            "fi\n"
        )
        executable.chmod(0o755)
    log = tmp_path / "calls.txt"
    env = {
        **os.environ,
        "PATH": str(commands) + os.pathsep + os.environ["PATH"],
        "KIND_TEST_CALL_LOG": str(log),
    }
    env.pop("SREGYM_BASELINE_CACHE_PATH", None)
    env.pop("SREGYM_CACHE_DIR", None)

    def run(script, *args, extra_env=None):
        result = subprocess.run(
            ["/bin/bash", str(scripts / script), *args],
            env={**env, **(extra_env or {})},
            capture_output=True,
            text=True,
        )
        calls = log.read_text().splitlines() if log.exists() else []
        return result, calls

    return run


@pytest.mark.parametrize("args", [(), ("auto",), ("arm",), ("x86",)])
def test_kind_setup_uses_shared_config_for_default_and_legacy_aliases(shell_environment, args):
    result, calls = shell_environment("setup_kind_cluster.sh", *args)
    assert result.returncode == 0, result.stderr
    assert calls[0].startswith("rm -f ")
    assert calls[1].startswith("kind create cluster --config ")
    assert calls[1].endswith("kind-config.yaml")
    assert any("rollout status daemonset/calico-node -n kube-system --timeout=900s" in call for call in calls)
    assert any("kubectl wait --for=condition=Ready nodes --all" in call for call in calls)
    assert any("kubectl run sregym-cni-probe-" in call for call in calls)
    assert any("kubectl wait --for=condition=Ready pod/sregym-cni-probe-" in call for call in calls)


def test_kind_probe_covers_each_node_and_removes_all_probes(shell_environment):
    result, calls = shell_environment("setup_kind_cluster.sh")
    assert result.returncode == 0, result.stderr
    creates = [call for call in calls if call.startswith("kubectl run sregym-cni-probe-")]
    waits = [call for call in calls if call.startswith("kubectl wait --for=condition=Ready pod/sregym-cni-probe-")]
    deletes = [call for call in calls if call.startswith("kubectl delete pod sregym-cni-probe-")]
    assert len(creates) == len(waits) == 2
    assert '"nodeName":"worker-a"' in creates[0]
    assert '"nodeName":"worker-b"' in creates[1]
    assert calls.index(creates[1]) < calls.index(waits[0])
    assert len(deletes) == 1
    assert all(create.split()[2] in deletes[0] for create in creates)


@pytest.mark.parametrize("operation", ["run", "wait", "delete", "interrupt"])
def test_kind_probe_errors_clean_up_attempted_pods(shell_environment, operation):
    result, calls = shell_environment("setup_kind_cluster.sh", extra_env={"KIND_TEST_FAIL_PROBE": operation})
    assert result.returncode != 0
    assert any(call.startswith("kubectl delete pod sregym-cni-probe-") for call in calls)
    if operation == "wait":
        assert any(call.startswith("kubectl describe pod sregym-cni-probe-") for call in calls)
        assert any("get events" in call and "involvedObject.name=" in call for call in calls)


def test_kind_probe_rejects_an_empty_node_list(shell_environment):
    result, calls = shell_environment("setup_kind_cluster.sh", extra_env={"KIND_TEST_NO_PROBE_NODES": "true"})
    assert result.returncode != 0
    assert "no uncordoned Linux nodes" in result.stdout
    assert not any(call.startswith("kubectl run ") for call in calls)


def test_unknown_argument_fails_before_creating_cluster(shell_environment):
    result, calls = shell_environment("setup_kind_cluster.sh", "unknown")
    assert result.returncode != 0
    assert not calls


def test_kind_setup_accepts_dind_config_and_node_image(shell_environment, tmp_path):
    config = tmp_path / "dind-kind.yaml"
    config.touch()
    result, calls = shell_environment(
        "setup_kind_cluster.sh",
        "arm",
        extra_env={
            "KIND_CONFIG": str(config),
            "KIND_NODE_IMAGE": "sregym-kind:local",
            "KIND_RETAIN_ON_FAILURE": "true",
        },
    )
    assert result.returncode == 0, result.stderr
    assert calls[1] == f"kind create cluster --config {config} --image sregym-kind:local --retain"


def test_kind_setup_names_the_cluster_from_the_environment(shell_environment):
    result, calls = shell_environment("setup_kind_cluster.sh", extra_env={"KIND_CLUSTER_NAME": "sregym-2"})
    assert result.returncode == 0, result.stderr
    assert calls[1].startswith("kind create cluster --config ")
    assert calls[1].endswith(" --name sregym-2")
    # Every later kind command must address the same cluster, or it acts on
    # the default one.
    assert "kind get nodes --name sregym-2" in calls


def test_kind_setup_clears_baseline_in_default_cache_dir(shell_environment):
    result, calls = shell_environment("setup_kind_cluster.sh")
    assert result.returncode == 0, result.stderr
    home = os.environ["HOME"]
    assert f"rm -f {home}/cache_dir/cluster_baseline_state.json" in calls


def test_kind_setup_clears_baseline_in_sregym_cache_dir(shell_environment, tmp_path):
    cache_dir = tmp_path / "cache"
    result, calls = shell_environment("setup_kind_cluster.sh", extra_env={"SREGYM_CACHE_DIR": str(cache_dir)})
    assert result.returncode == 0, result.stderr
    assert f"rm -f {cache_dir}/cluster_baseline_state.json" in calls


@pytest.mark.parametrize("cache_setting", ["default", "cache-dir", "baseline-path"])
def test_kind_setup_invalidates_baseline_before_cluster_creation(shell_environment, tmp_path, cache_setting):
    home = tmp_path / "home"
    baseline = home / "cache_dir" / "cluster_baseline_state.json"
    extra_env = {"HOME": str(home), "KIND_TEST_FAIL": "kind"}
    if cache_setting == "cache-dir":
        cache_dir = tmp_path / "custom cache"
        baseline = cache_dir / "cluster_baseline_state.json"
        extra_env["SREGYM_CACHE_DIR"] = str(cache_dir)
    elif cache_setting == "baseline-path":
        baseline = tmp_path / "custom_baseline.json"
        extra_env["SREGYM_BASELINE_CACHE_PATH"] = str(baseline)
        extra_env["SREGYM_CACHE_DIR"] = str(tmp_path / "other-cache")

    result, calls = shell_environment(
        "setup_kind_cluster.sh",
        extra_env=extra_env,
    )

    assert result.returncode != 0
    assert len(calls) == 2
    assert calls[0] == f"rm -f {baseline}"
    assert calls[1].startswith("kind create cluster --config ")
