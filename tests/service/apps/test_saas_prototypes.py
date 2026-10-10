"""Admission constraints that must remain true as the prototype manifests evolve."""

import hashlib
import io
import json
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from sregym.conductor.problems.wrong_service_selector import WrongServiceSelector
from sregym.service.apps.gitlab.application import GitLabCE
from sregym.service.apps.gitlab.recovery import GitLabRecovery
from sregym.service.apps.mattermost.application import Mattermost
from sregym.service.apps.stripe.application import StripeMarathon


@pytest.mark.parametrize("cls", [GitLabCE, Mattermost, StripeMarathon])
@pytest.mark.parametrize("tier,members", [("single", 1), ("replicated", 3)])
def test_persistent_topology_and_secret_references(cls, tier, members):
    app = cls(tier)
    docs = app.render()
    pg = next(d for d in docs if d["kind"] == "Cluster")["spec"]
    assert pg["instances"] == members
    assert 0 < pg["smartShutdownTimeout"] < pg["stopDelay"] < 180
    assert pg["storage"]["storageClass"] == "standard"
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
    from sregym.conductor.oracles.saas import SaaSOracle

    problem = WrongServiceSelector(app_name=name, faulty_service=slug, scale_tier="replicated")
    assert isinstance(problem.mitigation_oracle, SaaSOracle)
    assert problem.expected_service_port == problem.app.frontend_port
    assert problem.app.members == 3


def test_imported_reference_and_tests_match_pinned_source():
    root = Path(__file__).resolve().parents[3] / "sregym/service/apps/fixtures/swe-marathon-stripe"
    manifest = json.loads((root / "upstream.json").read_text())
    assert manifest["license"] == "Apache-2.0"
    assert len([r for r in manifest["files"] if r["path"].startswith("tests/test_")]) == 12
    for record in manifest["files"]:
        assert hashlib.sha256((root / record["path"]).read_bytes()).hexdigest() == record["sha256"]


def test_rejects_unsupported_tier():
    with pytest.raises(ValueError):
        Mattermost("expanded")


@pytest.mark.parametrize(
    "cls,method,mode,expected_auth,expected_result",
    [
        (GitLabCE, "run_client", "seed", ("private-token", "dummy-token"), {"project": 1}),
        (GitLabRecovery, "recovery_client", "git", ("private-token", "dummy-token"), {}),
        (
            Mattermost,
            "run_client",
            "seed",
            ("authorization", "Bearer dummy-login"),
            {"team": "record-id", "channel": "record-id"},
        ),
        (StripeMarathon, "run_client", "seed", ("authorization", "Bearer dummy-token"), {"endpoint": 1}),
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
        data = [] if "/projects?owned=true" in request.full_url else {"id": "record-id" if cls is Mattermost else 1}
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
    authenticated = requests[2:] if cls is Mattermost else requests
    assert authenticated
    assert all(dict((k.lower(), v) for k, v in request.header_items())[header] == value for request in authenticated)
    assert all(request.full_url.startswith(f"http://{app.slug}") for request in requests)
