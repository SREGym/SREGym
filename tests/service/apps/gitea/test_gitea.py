import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.service.apps.gitea import workflow as workload
from sregym.service.apps.gitea.application import GiteaApplication
from sregym.service.apps.persistent import cleanup_persistent_namespace


def test_agent_application_endpoint_exposes_gitea_and_its_tier(monkeypatch):
    from sregym.conductor import conductor_api

    app = GiteaApplication("replicated")
    monkeypatch.setattr(conductor_api, "_conductor", SimpleNamespace(app=app))
    payload = asyncio.run(conductor_api.get_app())
    assert payload["app_name"] == "Gitea"
    assert payload["namespaces"] == ["gitea"]
    assert "3 PostgreSQL members" in payload["descriptions"]


@pytest.mark.parametrize("tier,members", [("single", 1), ("replicated", 3)])
def test_tiers_keep_one_repository_writer_and_use_dedicated_database_storage(tier, members):
    documents = GiteaApplication(tier).render()
    database = next(d for d in documents if d["kind"] == "Cluster")["spec"]
    application = next(d for d in documents if d["kind"] == "Deployment")["spec"]
    assert database["instances"] == members
    assert database["storage"] == {"size": "2Gi", "storageClass": "openebs-hostpath"}
    assert application["replicas"] == 1
    assert application["strategy"]["type"] == "Recreate"
    assert application["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] == "gitea-repositories"
    if members > 1:
        assert database["postgresql"]["synchronous"]["dataDurability"] == "required"
        assert database["postgresql"]["synchronous"]["number"] == 1
    else:
        assert "synchronous" not in database["postgresql"]


def test_cleanup_waits_only_for_owned_reclaimable_volumes():
    volumes = [
        {
            "metadata": {"name": name},
            "spec": {"claimRef": {"namespace": namespace}, "persistentVolumeReclaimPolicy": policy},
        }
        for name, namespace, policy in [
            ("ours", "gitea", "Delete"),
            ("retained", "gitea", "Retain"),
            ("unrelated", "other", "Delete"),
        ]
    ]
    app = SimpleNamespace(namespace="gitea", command=Mock(side_effect=[json.dumps({"items": volumes}), "", ""]))
    cleanup_persistent_namespace(app)
    calls = app.command.call_args_list
    assert calls[1].args[:3] == ("delete", "namespace", "gitea")
    assert calls[2].args == ("wait", "--for=delete", "pv/ours", "--timeout=180s")


def test_native_git_probe_rejects_missing_acknowledged_content():
    app = GiteaApplication()
    app.run_client = Mock(return_value={"token": "a" * 32, "issue_number": 1})
    app.command = Mock(return_value="corrupted")
    with pytest.raises(RuntimeError, match="missing from the Git repository"):
        app.check_workflow()


def test_workload_defaults_apply_the_configured_rate_and_allow_overrides(monkeypatch):
    from sregym.service.apps.gitea import application

    wrk = Mock()
    monkeypatch.setattr(application, "Wrk2", wrk)
    monkeypatch.setattr(application, "Wrk2WorkloadManager", Mock())
    app = GiteaApplication()

    app.create_workload()
    wrk.assert_called_with(rate=10, namespace="gitea", dist="fixed", duration=30)

    app.create_workload(rate=25, dist="exp", duration=60, connections=4)
    wrk.assert_called_with(rate=25, namespace="gitea", dist="exp", duration=60, connections=4)


def test_probe_and_verification_use_the_same_issue_and_repository_path(monkeypatch, capsys):
    issue = {"number": 1, "title": "change-abc123"}
    api = Mock(return_value=issue)
    write = Mock()
    read = Mock(return_value="abc123")
    monkeypatch.setattr(workload, "api", api)
    monkeypatch.setattr(workload, "write_file", write)
    monkeypatch.setattr(workload, "read_file", read)

    workload.run("probe", token="abc123")
    result = json.loads(capsys.readouterr().out)
    api.assert_any_call("/repos/zoo-labs/zoo-utilities/issues", "POST", {"title": "change-abc123", "body": "abc123"})
    write.assert_called_once_with("zoo-labs/zoo-utilities", "changes/abc123.txt", "abc123")
    workload.run("verify", **result, fixture={"repositories": []})
    assert json.loads(capsys.readouterr().out) == {"success": True}
    assert read.call_args_list[0] == read.call_args_list[1]

    app = GiteaApplication()
    app.run_client = Mock(return_value=result)
    app.command = Mock(return_value="abc123")
    assert app.check_workflow(token="abc123") == result
    assert app.command.call_args.args[-1] == "HEAD:changes/abc123.txt"
