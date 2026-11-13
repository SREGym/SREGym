import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from sregym.paths import BASE_DIR
from sregym.service.telemetry import loki as loki_module
from sregym.service.telemetry.loki import Loki
from sregym.service.telemetry.prometheus import Prometheus


def _load_json(path):
    return json.loads(Path(path).read_text())


def _load_yaml(path):
    return yaml.safe_load(Path(path).read_text())


def test_prometheus_storage_is_managed_by_its_helm_chart():
    prometheus = Prometheus()
    metadata = _load_json(prometheus.config_file)
    values = _load_yaml(Path(prometheus.helm_configs["chart_path"]) / "values.yaml")
    persistence = values["server"]["persistentVolume"]

    assert "PersistentVolumeClaimConfig" not in metadata
    assert persistence["enabled"] is True
    assert persistence["existingClaim"] == ""
    assert persistence["storageClass"] == "openebs-hostpath"


def test_loki_storage_is_managed_by_its_helm_chart():
    loki = Loki()
    metadata = _load_json(loki.config_file)
    values = _load_yaml(BASE_DIR / "observer/loki/loki-values.yaml")
    persistence = values["singleBinary"]["persistence"]

    assert "PersistentVolumeClaimConfig" not in metadata
    assert persistence["enabled"] is True
    assert persistence["storageClass"] == "openebs-hostpath"
    assert persistence["size"] == "10Gi"


def test_loki_updates_only_grafana_repo_with_bounded_commands(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(loki_module.subprocess, "run", run)
    Loki()._add_grafana_helm_repo()

    assert calls == [
        (
            ["helm", "repo", "add", "grafana", "https://grafana.github.io/helm-charts"],
            {"check": True, "capture_output": True, "text": True, "timeout": 30},
        ),
        (
            ["helm", "repo", "update", "grafana"],
            {"check": True, "capture_output": True, "text": True, "timeout": 60},
        ),
    ]


@pytest.mark.parametrize("operation", ["add", "update"])
@pytest.mark.parametrize("failure", ["exit", "timeout", "missing_binary"])
def test_loki_repository_errors_do_not_skip_refresh_or_escape(monkeypatch, caplog, operation, failure):
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        if command[2] == operation:
            if failure == "exit":
                raise subprocess.CalledProcessError(1, command, stderr="repository command rejected")
            if failure == "timeout":
                raise subprocess.TimeoutExpired(command, 30 if operation == "add" else 60)
            raise FileNotFoundError("helm executable is missing")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(loki_module.subprocess, "run", run)

    Loki()._add_grafana_helm_repo()

    assert [command[2] for command in commands] == ["add", "update"]
    assert f"Helm repo {operation} failed" in caplog.text
    if failure == "exit":
        assert "repository command rejected" in caplog.text


@pytest.mark.parametrize("repository_error", [False, True])
def test_loki_deploy_keeps_repository_setup_before_install(monkeypatch, repository_error):
    calls = []
    loki = Loki()
    monkeypatch.setattr(loki, "_is_loki_running", lambda: False)
    monkeypatch.setattr(loki, "_deploy_promtail", lambda: calls.append("promtail"))
    monkeypatch.setattr(loki_module.Helm, "uninstall", lambda **_kwargs: calls.append("uninstall"))
    monkeypatch.setattr(loki_module.Helm, "install", lambda **_kwargs: calls.append("install"))
    monkeypatch.setattr(loki_module.Helm, "assert_if_deployed", lambda _namespace: calls.append("ready"))

    def run(command, **_kwargs):
        calls.append(command[2])
        if repository_error:
            raise subprocess.CalledProcessError(1, command, stderr="temporary repository failure")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(loki_module.subprocess, "run", run)

    loki.deploy()

    assert calls == ["uninstall", "add", "update", "install", "ready", "promtail"]


def test_running_loki_does_not_refresh_repositories(monkeypatch):
    loki = Loki()
    refresh = Mock()
    promtail = Mock()
    monkeypatch.setattr(loki, "_is_loki_running", lambda: True)
    monkeypatch.setattr(loki, "_add_grafana_helm_repo", refresh)
    monkeypatch.setattr(loki, "_deploy_promtail", promtail)

    loki.deploy()

    refresh.assert_not_called()
    promtail.assert_called_once_with()
