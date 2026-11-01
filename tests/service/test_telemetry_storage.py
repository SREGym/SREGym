import json
from pathlib import Path

import yaml

from sregym.paths import BASE_DIR
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

    class Kubectl:
        def exec_command_checked(self, command, timeout=None):
            calls.append((command, timeout))

    monkeypatch.setattr("sregym.service.telemetry.loki.KubeCtl", Kubectl)
    Loki()._add_grafana_helm_repo()

    assert calls == [
        ("helm repo add grafana https://grafana.github.io/helm-charts", 30),
        ("helm repo update grafana", 60),
    ]
