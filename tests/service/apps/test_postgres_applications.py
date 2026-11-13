"""Admission constraints that must remain true as the prototype manifests evolve."""

import hashlib
import io
import json
import sqlite3
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock

import pytest

from sregym.conductor.problems.wrong_service_selector import WrongServiceSelector
from sregym.service.apps.components.http_client import Client
from sregym.service.apps.gitlab.application import GitLabApplication
from sregym.service.apps.gitlab.recovery import GitLabRecoveryApplication
from sregym.service.apps.mattermost.application import MattermostApplication
from sregym.service.apps.mattermost.workflow import workflow as mattermost_workflow
from sregym.service.apps.stripe.application import StripeApplication

pytestmark = pytest.mark.usefixtures("offline_kubectl")


@pytest.mark.parametrize("cls", [GitLabApplication, MattermostApplication, StripeApplication])
@pytest.mark.parametrize("tier,members", [("single", 1), ("replicated", 3)])
def test_persistent_topology_and_secret_references(cls, tier, members):
    app = cls(tier)
    docs = app.render()
    pg = next(d for d in docs if d["kind"] == "Cluster")["spec"]
    assert pg["instances"] == members
    assert 0 < pg["smartShutdownTimeout"] < pg["stopDelay"] < 180
    assert pg["storage"]["storageClass"] == "openebs-hostpath"
    if members > 1:
        assert pg["postgresql"]["synchronous"]["dataDurability"] == "required"
    assert sum(d["kind"] == "PersistentVolumeClaim" for d in docs) + members == app.expected_volume_count
    deployment = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == app.slug)
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["progressDeadlineSeconds"] > app.startup_timeout
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    assert "secretKeyRef" in json.dumps(deployment)
    assert not deployment["spec"]["template"]["spec"]["automountServiceAccountToken"]


@pytest.mark.parametrize(
    "name,slug", [("gitlab_ce", "gitlab-ce"), ("mattermost", "mattermost"), ("stripe_marathon", "stripe-marathon")]
)
def test_existing_problem_selects_the_business_oracle(name, slug):
    from sregym.conductor.oracles.postgres_application import PostgresApplicationOracle

    problem = WrongServiceSelector(app_name=name, faulty_service=slug, scale_tier="replicated")
    assert isinstance(problem.mitigation_oracle, PostgresApplicationOracle)
    assert problem.expected_service_port == problem.app.frontend_port
    assert problem.app.members == 3


def test_imported_reference_and_tests_match_pinned_source():
    root = Path(__file__).resolve().parents[3] / "docker/apps/stripe/backend/vendor"
    manifest = json.loads((root / "upstream.json").read_text())
    assert manifest["license"] == "Apache-2.0"
    assert len([r for r in manifest["files"] if r["path"].startswith("tests/test_")]) == 12
    for record in manifest["files"]:
        assert hashlib.sha256((root / record["path"]).read_bytes()).hexdigest() == record["sha256"]


def test_rejects_unsupported_tier():
    with pytest.raises(ValueError):
        MattermostApplication("expanded")


@pytest.mark.parametrize(
    "cls,method,mode,expected_auth,expected_result",
    [
        (GitLabApplication, "run_client", "seed", ("private-token", "dummy-token"), {"project": 1}),
        (GitLabRecoveryApplication, "recovery_client", "git", ("private-token", "dummy-token"), {}),
        (
            MattermostApplication,
            "run_client",
            "seed",
            ("authorization", "Bearer dummy-login"),
            {"team": "record-id", "channel": "record-id"},
        ),
        (StripeApplication, "run_client", "seed", ("authorization", "Bearer dummy-token"), {"endpoint": 1}),
    ],
)
def test_rendered_client_executes_with_app_specific_authentication(
    monkeypatch, cls, method, mode, expected_auth, expected_result
):
    app = cls("single")
    original_read = Path.read_text

    def read(path, *args, **kwargs):
        credentials = {"/credentials/password": "dummy-password", "/credentials/token": "dummy-token"}
        return credentials[str(path)] if str(path) in credentials else original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    requests = []

    def urlopen(request, **kwargs):
        requests.append(request)
        data = (
            []
            if "/projects?owned=true" in request.full_url
            else {"id": "record-id" if cls is MattermostApplication else 1}
        )
        response = io.BytesIO(json.dumps(data).encode())
        response.headers = {"Token": "dummy-login"}
        return response

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    def command(*args, input_text, **kwargs):
        assert args == ("exec", "-i", "application-client", "--", "python", "-")
        # Execute the exact source passed to the client pod, not a Python import
        # that could hide a missing dependency in the assembled script.
        output = io.StringIO()
        with redirect_stdout(output):
            exec(compile(input_text, "application-client", "exec"), {})
        return output.getvalue()

    monkeypatch.setattr(app, "command", command)
    assert getattr(app, method)(mode) == expected_result
    header, value = expected_auth
    authenticated = requests[2:] if cls is MattermostApplication else requests
    assert authenticated
    assert all(dict((k.lower(), v) for k, v in request.header_items())[header] == value for request in authenticated)
    assert all(request.full_url.startswith(f"http://{app.slug}") for request in requests)


def test_mattermost_probe_verification_and_replica_query_use_the_same_record():
    user, team, channel = {"id": "user-id"}, {"id": "team-id"}, {"id": "channel-id"}
    post = {
        "id": "post-id",
        "message": "change-abc123",
        "channel_id": channel["id"],
        "file_ids": ["file-id"],
        "delete_at": 0,
    }
    client = Mock(password="dummy-password", headers={}, response_headers={"Token": "dummy-token"})
    client.require = Client.require
    client.call.side_effect = [
        user,
        team,
        channel,
        {"file_infos": [{"id": "file-id"}]},
        post,
        user,
        team,
        channel,
        post,
        b"abc123",
    ]
    result = mattermost_workflow(client, "probe", token="abc123")
    assert mattermost_workflow(client, "verify", **result) == {"verified": True}
    client.call.assert_any_call("GET", "/api/v4/teams/name/operations")
    client.call.assert_any_call(
        "POST", "/api/v4/users/login", {"login_id": "operations-bot", "password": "dummy-password"}
    )
    with sqlite3.connect(":memory:") as database:
        database.execute("CREATE TABLE posts (message TEXT, deleteat INTEGER)")
        database.execute("INSERT INTO posts VALUES (?, ?)", (post["message"], post["delete_at"]))
        assert database.execute(MattermostApplication().record_query(result["token"])).fetchone()[0] == 1
