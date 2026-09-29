import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.service.apps.gitea import Gitea
from sregym.service.apps.persistent import cleanup_persistent_namespace


def test_agent_application_endpoint_exposes_gitea_and_its_tier(monkeypatch):
    from sregym.conductor import conductor_api

    app = Gitea("replicated")
    monkeypatch.setattr(conductor_api, "_conductor", SimpleNamespace(app=app))
    payload = asyncio.run(conductor_api.get_app())
    assert payload["app_name"] == "Gitea"
    assert payload["namespaces"] == ["gitea"]
    assert "3 PostgreSQL members" in payload["descriptions"]


@pytest.mark.parametrize("tier,members", [("single", 1), ("replicated", 3)])
def test_tiers_keep_one_repository_writer_and_use_dedicated_database_storage(tier, members):
    documents = Gitea(tier).render()
    database = next(d for d in documents if d["kind"] == "Cluster")["spec"]
    application = next(d for d in documents if d["kind"] == "Deployment")["spec"]
    assert database["instances"] == members
    assert database["storage"] == {"size": "2Gi", "storageClass": "standard"}
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
    app = Gitea()
    app.run_client = Mock(return_value={"token": "a" * 32, "issue_number": 1})
    app.command = Mock(return_value="corrupted")
    with pytest.raises(RuntimeError, match="missing from the Git repository"):
        app.check_workflow()
