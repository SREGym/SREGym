import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.service.apps import helpers
from sregym.service.apps.components.client_runner import PythonClientRunner
from sregym.service.apps.components.cnpg import PostgresCluster
from sregym.service.apps.components.kubernetes import KubernetesCommands


def test_command_preserves_namespace_input_timeout_and_checked_exit(monkeypatch):
    from sregym.service.apps.components import kubernetes

    run = Mock(return_value=SimpleNamespace(stdout="result"))
    monkeypatch.setattr(kubernetes.subprocess, "run", run)
    app = KubernetesCommands()
    app.namespace = "application"
    assert app.command("apply", "-f", "-", input_text="document", timeout=180) == "result"
    run.assert_called_once_with(
        ["kubectl", "-n", "application", "apply", "-f", "-"],
        input="document",
        capture_output=True,
        text=True,
        check=True,
        timeout=180,
    )


def test_client_sources_execute_on_stdin_without_host_imports(tmp_path):
    source = tmp_path / "workflow.py"
    source.write_text("import json\ndef workflow(): return {'success': True}\n")
    command = Mock(return_value='{"success":true}')
    assert PythonClientRunner(command).run([source], "print(json.dumps(workflow()))") == {"success": True}
    assert command.call_args.args == ("exec", "-i", "application-client", "--", "python", "-")
    assert "print(json.dumps(workflow()))" in command.call_args.kwargs["input_text"]
    assert command.call_args.kwargs["timeout"] == 180


def test_postgres_inspection_keeps_only_live_database_members():
    pods = [
        {"metadata": {"name": "ready"}, "spec": {"containers": [{"name": "postgres"}]}},
        {"metadata": {"name": "gone", "deletionTimestamp": "now"}, "spec": {"containers": [{"name": "postgres"}]}},
        {"metadata": {"name": "other"}, "spec": {"containers": [{"name": "client"}]}},
    ]
    command = Mock(return_value=json.dumps({"items": pods}))
    cluster = PostgresCluster(command, "database", "records")
    assert [pod["metadata"]["name"] for pod in cluster.pods()] == ["ready"]
    assert cluster.document({"instances": 3})["spec"] == {"instances": 3}


def test_postgres_sql_preserves_strict_psql_arguments():
    command = Mock(return_value="1\n")
    assert PostgresCluster(command, "database", "records").sql("SELECT 1", pod="primary") == "1"
    command.assert_called_once_with(
        "exec",
        "-i",
        "primary",
        "-c",
        "postgres",
        "--",
        "psql",
        "-U",
        "postgres",
        "-d",
        "records",
        "-X",
        "-A",
        "-t",
        "-v",
        "ON_ERROR_STOP=1",
        input_text="SELECT 1",
        timeout=45,
    )


def test_image_catalog_preserves_current_published_references():
    assert helpers.image_reference("mattermost-server").startswith("ghcr.io/sregym/mattermost:11.7.0@sha256:")
    assert helpers.image_reference("stripe-backend").startswith("ghcr.io/sregym/stripe-marathon:20261010@sha256:")


@pytest.mark.parametrize("catalog,reason", [({}, "not recorded"), ({"image": "example:latest"}, "pinned SHA256")])
def test_image_resolution_fails_without_a_pin(monkeypatch, catalog, reason):
    monkeypatch.setattr(helpers, "_image_catalog", lambda: catalog)
    with pytest.raises(ValueError, match=reason):
        helpers.image_reference("image")
