import copy
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from sregym.generators.fault import inject_virtual
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector


@pytest.fixture(
    params=[
        ("hotel-reservation", "mongodb-geo", "mongo-geo-script"),
        ("social-network", "media-mongodb", "media-mongodb"),
    ]
)
def recovery_case(request, tmp_path, monkeypatch):
    namespace, service, name = request.param
    manifest = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "resourceVersion": "old-version",
            "uid": "old-uid",
            "creationTimestamp": "2026-10-26T00:00:00Z",
            "managedFields": [{"manager": "kubectl"}],
            "labels": {"app": "database"},
            "annotations": {
                "example.com/setting": "original",
                "kubectl.kubernetes.io/last-applied-configuration": "stale metadata",
            },
        },
        "data": {"init.js": "original configuration"},
        "binaryData": {"extra": "aGVsbG8="},
    }
    backup = tmp_path / f"{service}_modified.yaml"
    backup.write_text(yaml.safe_dump(manifest))
    monkeypatch.setattr(inject_virtual, "Path", lambda path: tmp_path / Path(path).name)
    injector = VirtualizationFaultInjector.__new__(VirtualizationFaultInjector)
    injector.namespace = namespace
    injector.kubectl = Mock()
    return injector, service, manifest, backup


def test_injection_saves_reapplicable_configmap(recovery_case):
    injector, service, manifest, _ = recovery_case
    original = copy.deepcopy(manifest)
    injector.kubectl.exec_command.return_value = yaml.safe_dump(manifest)
    injector._write_yaml_to_file = Mock()

    injector.inject_missing_configmap([service])

    saved_service, saved = injector._write_yaml_to_file.call_args.args
    assert saved_service == service
    assert saved["data"] == manifest["data"]
    assert saved["binaryData"] == manifest["binaryData"]
    assert saved["metadata"] == {
        "name": manifest["metadata"]["name"],
        "namespace": injector.namespace,
        "labels": {"app": "database"},
        "annotations": {"example.com/setting": "original"},
    }
    assert manifest == original


def test_recovery_cleans_legacy_backup_and_checks_each_command(recovery_case, capsys):
    injector, service, manifest, backup = recovery_case
    original_backup = backup.read_text()

    injector.recover_missing_configmap([service])
    injector.recover_missing_configmap([service])

    calls = injector.kubectl.exec_command_checked.call_args_list
    assert len(calls) == 6
    for offset in (0, 3):
        assert calls[offset].args[0] == f"kubectl apply -f - -n {injector.namespace}"
        restored = yaml.safe_load(calls[offset].kwargs["input_data"])
        assert restored["data"] == manifest["data"]
        assert restored["binaryData"] == manifest["binaryData"]
        assert restored["metadata"] == {
            "name": manifest["metadata"]["name"],
            "namespace": injector.namespace,
            "labels": {"app": "database"},
            "annotations": {"example.com/setting": "original"},
        }
        assert calls[offset + 1].args[0] == f"kubectl rollout restart deployment {service} -n {injector.namespace}"
        assert calls[offset + 2].args[0] == (
            f"kubectl rollout status deployment {service} -n {injector.namespace} --timeout=120s"
        )
        assert [call.kwargs["timeout"] for call in calls[offset : offset + 3]] == [30, 30, 130]
    assert backup.read_text() == original_backup
    injector.kubectl.exec_command.assert_not_called()
    assert f"Restored ConfigMap: {manifest['metadata']['name']}" in capsys.readouterr().out


@pytest.mark.parametrize("failed_step", [0, 1, 2], ids=["apply", "restart", "rollout"])
def test_recovery_does_not_report_success_after_command_failure(recovery_case, failed_step, capsys):
    injector, service, _, backup = recovery_case
    original_backup = backup.read_text()
    injector.kubectl.exec_command_checked.side_effect = [""] * failed_step + [RuntimeError("API rejected command")]

    with pytest.raises(RuntimeError, match="API rejected command"):
        injector.recover_missing_configmap([service])

    assert injector.kubectl.exec_command_checked.call_count == failed_step + 1
    assert "recovered" not in capsys.readouterr().out
    assert backup.read_text() == original_backup


def test_recovery_stops_before_commands_when_backup_is_missing(recovery_case):
    injector, service, _, backup = recovery_case
    backup.unlink()

    with pytest.raises(FileNotFoundError):
        injector.recover_missing_configmap([service])

    injector.kubectl.exec_command_checked.assert_not_called()
