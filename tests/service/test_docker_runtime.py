import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.service import docker_runtime as runtime


def test_default_docker_commands_are_unchanged():
    assert runtime.docker_command("info") == ["docker", "info"]


@pytest.mark.parametrize("address", ["127.0.0.1", "0.0.0.0", "::1", "localhost", "224.0.0.1", ""])
def test_rootless_runner_requires_reachable_address(monkeypatch, address):
    monkeypatch.setenv("SREGYM_RUNNER_ADDRESS", address)
    with pytest.raises(ValueError, match="IPv4"):
        runtime.runner_address()


@pytest.mark.skipif(os.name != "posix", reason="Linux rootless deployment")
@pytest.mark.parametrize("violation", [None, "same_uid", "root_uid", "same_engine", "rootful", "cgroup1", "no_cache"])
def test_boundary_preflight_rejects_shared_or_unqualified_configuration(monkeypatch, violation):
    monkeypatch.setenv("DOCKER_HOST", "unix:///workload.sock")
    monkeypatch.setenv("SREGYM_TRUSTED_DOCKER_HOST", "unix:///trusted.sock")
    monkeypatch.setenv("SREGYM_RUNNER_ADDRESS", "192.0.2.10")
    monkeypatch.setenv("SREGYM_CLUSTER_BASELINE_FILE", "/private/rootless-baseline.json")
    if violation == "no_cache":
        monkeypatch.delenv("SREGYM_CLUSTER_BASELINE_FILE")
    owner = os.getuid() + 1
    if violation == "same_uid":
        owner = os.getuid()
    elif violation == "root_uid":
        owner = 0
    monkeypatch.setattr(Path, "stat", lambda _: SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660, st_uid=owner))
    probe = Mock()
    probe.__enter__ = Mock(return_value=probe)
    probe.__exit__ = Mock()
    monkeypatch.setattr(runtime.socket, "socket", Mock(return_value=probe))
    monkeypatch.setattr(runtime, "_validate_workload_cluster", Mock(return_value=["node"]))
    monkeypatch.setattr(runtime, "_validate_rootless_inotify_budget", Mock(return_value=512))
    workload = {"ID": "workload", "SecurityOptions": ["name=rootless"], "CgroupVersion": "2"}
    if violation == "rootful":
        workload["SecurityOptions"] = []
    if violation == "cgroup1":
        workload["CgroupVersion"] = "1"
    trusted = {"ID": "workload" if violation == "same_engine" else "trusted"}
    run = Mock(side_effect=[subprocess.CompletedProcess([], 0, json.dumps(info)) for info in (workload, trusted)])
    monkeypatch.setattr(runtime.subprocess, "run", run)
    if violation:
        with pytest.raises(ValueError):
            runtime.validate_rootless_boundary()
    else:
        assert runtime.validate_rootless_boundary()["workload_uid"] == owner
        assert [call.args[0][2] for call in run.call_args_list] == ["unix:///workload.sock", "unix:///trusted.sock"]


@pytest.mark.parametrize("value", ["128", "511", "0", "unreadable"])
def test_rootless_preflight_rejects_insufficient_or_invalid_inotify_budget(monkeypatch, value):
    monkeypatch.setattr(Path, "read_text", lambda _: value)
    with pytest.raises(ValueError, match="inotify"):
        runtime._validate_rootless_inotify_budget()


@pytest.mark.parametrize("value", ["512", "1024\n"])
def test_rootless_preflight_records_sufficient_inotify_budget(monkeypatch, value):
    monkeypatch.setattr(Path, "read_text", lambda _: value)
    assert runtime._validate_rootless_inotify_budget() == int(value)


def test_rootless_preflight_fails_if_host_inotify_budget_cannot_be_read(monkeypatch):
    def denied(_):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(ValueError, match="inotify"):
        runtime._validate_rootless_inotify_budget()


@pytest.mark.skipif(os.name != "posix", reason="Linux private workload kubeconfig")
@pytest.mark.parametrize(
    "violation",
    [None, "public_config", "wrong_host", "no_tls", "no_nodes", "wrong_provider", "wrong_role", "wrong_port"],
)
def test_workload_kubeconfig_must_match_rootless_kind_engine(monkeypatch, tmp_path, violation):
    config = tmp_path / "kubeconfig"
    config.write_text("private operator config")
    config.chmod(0o644 if violation == "public_config" else 0o600)
    monkeypatch.setenv("KUBECONFIG", str(config))
    cluster = {"server": "https://192.0.2.10:44321"}
    if violation == "wrong_host":
        cluster["server"] = "https://127.0.0.1:12345"
    if violation == "no_tls":
        cluster["insecure-skip-tls-verify"] = True
    nodes = {"items": [{"metadata": {"name": "node"}, "spec": {"providerID": "kind://docker/test/node"}}]}
    if violation == "no_nodes":
        nodes["items"] = []
    if violation == "wrong_provider":
        nodes["items"][0]["spec"]["providerID"] = "other://privileged-node"
    container = [
        {
            "Config": {"Labels": {"io.x-k8s.kind.role": "control-plane"}},
            "NetworkSettings": {"Ports": {"6443/tcp": [{"HostIp": "192.0.2.10", "HostPort": "44321"}]}},
        }
    ]
    if violation == "wrong_role":
        container[0]["Config"]["Labels"] = {}
    if violation == "wrong_port":
        container[0]["NetworkSettings"]["Ports"]["6443/tcp"][0]["HostPort"] = "12345"
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, json.dumps(value))
            for value in ({"clusters": [{"cluster": cluster}]}, nodes, container)
        ]
    )
    monkeypatch.setattr(subprocess, "run", run)
    if violation:
        with pytest.raises(ValueError):
            runtime._validate_workload_cluster("unix:///workload.sock", "192.0.2.10")
    else:
        assert runtime._validate_workload_cluster("unix:///workload.sock", "192.0.2.10") == ["node"]
        assert run.call_args.args[0] == [
            "docker",
            "--host",
            "unix:///workload.sock",
            "inspect",
            "--type",
            "container",
            "node",
        ]
