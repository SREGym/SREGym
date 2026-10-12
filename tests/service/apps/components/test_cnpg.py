import hashlib
import io
import json
import subprocess
from unittest.mock import Mock

import pytest

from sregym.service.apps.components import cnpg


def operator(image=cnpg.OPERATOR_IMAGE, namespace="cnpg-system"):
    return {
        "metadata": {"name": "cnpg-controller-manager", "namespace": namespace},
        "spec": {"template": {"spec": {"containers": [{"image": image}]}}},
    }


def test_installs_verified_release_before_waiting(monkeypatch):
    manifest = b"verified manifest"
    download = Mock(return_value=io.BytesIO(manifest))
    monkeypatch.setattr(cnpg.urllib.request, "urlopen", download)
    monkeypatch.setattr(cnpg, "MANIFEST_SHA256", hashlib.sha256(manifest).hexdigest())
    command = Mock(side_effect=[json.dumps({"items": []}), json.dumps({"items": []}), "", ""])

    cnpg.ensure_operator(command)

    download.assert_called_once_with(cnpg.MANIFEST_URL, timeout=60)
    calls = command.call_args_list
    assert calls[2].args == ("apply", "-n", "cnpg-system", "--server-side", "-f", "-")
    assert calls[2].kwargs["input_text"] == manifest.decode()
    assert calls[3].args[:3] == ("rollout", "status", "deployment/cnpg-controller-manager")


def test_reuses_existing_pinned_operator_without_downloading(monkeypatch):
    download = Mock()
    monkeypatch.setattr(cnpg.urllib.request, "urlopen", download)
    command = Mock(side_effect=[json.dumps({"items": [operator()]}), ""])
    cnpg.ensure_operator(command)
    download.assert_not_called()
    assert [c.args[0] for c in command.call_args_list] == ["get", "rollout"]


@pytest.mark.parametrize(
    "operators",
    [
        [operator("ghcr.io/cloudnative-pg/cloudnative-pg:1.29.0")],
        [operator(namespace="existing-database-system")],
        [operator(), operator(namespace="other")],
    ],
)
def test_does_not_overwrite_another_installation(operators):
    command = Mock(return_value=json.dumps({"items": operators}))
    with pytest.raises(RuntimeError, match="different CloudNativePG"):
        cnpg.ensure_operator(command)
    assert command.call_count == 1


def test_does_not_take_over_orphaned_custom_resources():
    command = Mock(
        side_effect=[
            json.dumps({"items": []}),
            json.dumps({"items": [{"metadata": {"name": "clusters.postgresql.cnpg.io"}}]}),
        ]
    )
    with pytest.raises(RuntimeError, match="repair that installation"):
        cnpg.ensure_operator(command)
    assert command.call_count == 2


def test_does_not_apply_an_unverified_manifest(monkeypatch):
    monkeypatch.setattr(cnpg.urllib.request, "urlopen", Mock(return_value=io.BytesIO(b"modified release")))
    command = Mock(return_value=json.dumps({"items": []}))
    with pytest.raises(RuntimeError, match="checksum"):
        cnpg.ensure_operator(command)
    assert command.call_count == 2


def test_does_not_continue_when_cluster_read_fails():
    command = Mock(side_effect=subprocess.CalledProcessError(1, "kubectl"))
    with pytest.raises(subprocess.CalledProcessError):
        cnpg.ensure_operator(command)
    assert command.call_count == 1
