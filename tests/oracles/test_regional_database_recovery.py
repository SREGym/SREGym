"""Private oracle boundary, snapshot and actual protocol negative controls."""

import hashlib
import io
import json
import shutil
import subprocess
import threading
import time
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from sregym.conductor.oracles.codehub_state import (
    AcceptedOperation,
    EffectReceiptCut,
    ProtectedReceiptCut,
    ProtectedState,
    StateMismatch,
    canonical,
    digest,
)
from sregym.conductor.oracles.regional_database_recovery import (
    DeliveryObserverTarget,
    EvidenceUnavailable,
    FreshAPIChallenge,
    GitBundleExpectation,
    GitFileExpectation,
    GitProjectExpectation,
    GitRefExpectation,
    HTTPServiceTarget,
    RecoveryOutcomePlan,
    RegionalDatabaseRecoveryOracle,
    SQLTarget,
    WebhookDestination,
    port_forward,
    resolve_http_replicas,
)


def uid(number):
    return str(UUID(int=number))


def plan():
    targets = tuple(HTTPServiceTarget(f"region-{letter}", f"platform-{letter}", "api") for letter in "ab")
    project = GitProjectExpectation(
        uid(200),
        "user-secret-" + "u" * 32,
        (GitRefExpectation("refs/heads/main", "a" * 40),),
        (GitFileExpectation("a" * 40, "app.py", "b" * 64),),
        (GitBundleExpectation("a" * 40, "c" * 64),),
    )
    hook = WebhookDestination(
        uid(900), "http://customer-webhook/deliveries", ("issue.create", "issue.update", "repository.push")
    )
    return RecoveryOutcomePlan(
        targets,
        targets,
        targets,
        (project,),
        (FreshAPIChallenge("group-a", uid(100), uid(200), uid(1), "user-secret-" + "u" * 32, (hook,)),),
        DeliveryObserverTarget("http://127.0.0.1:8123/receipts", "private-read-" + "r" * 32),
        "service-secret-" + "s" * 32,
    )


def oracle():
    databases = tuple(
        SQLTarget(
            "group-a",
            f"region-{letter}",
            f"platform-{letter}",
            f"db-{letter}-{role}",
            "codehub",
            "readonly_observer",
            "sql-secret-" + "x" * 32,
        )
        for letter, role in (("a", "writer"), ("a", "reader"), ("b", "candidate"), ("b", "reader"))
    )
    cut = ProtectedReceiptCut("group-a", (0,), 1, (f"{uid(100)}/{uid(200)}",), "a" * 64, "b" * 64)
    return RegionalDatabaseRecoveryOracle(
        SimpleNamespace(),
        databases=databases,
        cuts=(cut,),
        effect_cuts=(EffectReceiptCut("group-a", 1, "c" * 64),),
        outcomes=plan(),
    )


def test_live_evaluation_has_no_host_fallback(monkeypatch):
    value = oracle()
    value.capture_baseline()
    monkeypatch.delenv("SREGYM_VERIFIER_CONTAINER", raising=False)
    verdict = value.evaluate()
    assert verdict["success"] is False
    assert verdict["reason"] == "recovery_container_required"
    assert verdict["failure_class"] == "harness_error"


def test_missing_independent_business_inventory_cannot_become_an_expected_solver_failure(monkeypatch):
    value = oracle()
    value.capture_baseline()
    value.outcomes = None
    monkeypatch.setenv("SREGYM_VERIFIER_CONTAINER", "1")
    verdict = value.evaluate()
    assert verdict["success"] is False
    assert verdict["reason"] == "recovery_snapshot_invalid"
    assert verdict["failure_class"] == "harness_error"


def test_missing_persistent_fresh_receipt_owner_fails_before_application_io(monkeypatch):
    value = oracle()
    value.capture_baseline()
    monkeypatch.setenv("SREGYM_VERIFIER_CONTAINER", "1")
    verdict = value.evaluate()
    assert verdict["reason"] == "recovery_evidence_unavailable"
    assert verdict["detail"]["check"] == "persistent_fresh_receipts"
    assert verdict["failure_class"] == "harness_error"


def test_baseline_capture_and_later_receipt_installation_do_not_rebase_expectations():
    value = oracle()
    value.capture_baseline()
    baseline = value.baseline_cuts
    with pytest.raises(RuntimeError, match="once"):
        value.capture_baseline()
    advanced = replace(value.cuts[0], closed_epochs=(0, 1), operations=2, journal_sha256="d" * 64)
    value.install_verification_snapshot(cuts=(advanced,), effect_cuts=value.effect_cuts, outcomes=value.outcomes)
    assert value.baseline_cuts is baseline
    assert value.baseline_cuts[0].journal_sha256 == "a" * 64
    with pytest.raises(ValueError, match="discard"):
        value.install_verification_snapshot(
            cuts=(replace(advanced, closed_epochs=(1,)),), effect_cuts=value.effect_cuts, outcomes=value.outcomes
        )
    assert value.cuts == (advanced,)


def test_incomplete_or_shared_database_inventory_fails_before_any_grade():
    value = oracle()
    value.databases = value.databases[:3]
    with pytest.raises(ValueError, match="distinct real"):
        value.capture_baseline()
    value = oracle()
    value.effect_cuts = (replace(value.effect_cuts[0], group="group-b"),)
    with pytest.raises(ValueError, match="match exactly"):
        value.capture_baseline()


def test_credentials_are_not_in_dto_repr():
    value = oracle()
    assert "sql-secret" not in repr(value.databases)
    assert "service-secret" not in repr(value.outcomes)
    assert "private-read" not in repr(value.outcomes)
    assert "user-secret" not in repr(value.outcomes)


def test_private_delivery_receipts_use_only_the_bounded_resource_pipe():
    value, requests = oracle(), []
    value.outcomes = replace(value.outcomes, observer=DeliveryObserverTarget("", "", transport="private_pipe"))

    def receipts(batch):
        requests.append(tuple(batch))
        return {"receipts": [{"effect_id": identity} for identity in batch]}

    value.fresh_journal = SimpleNamespace(delivery_receipts=receipts)
    identities = [f"{index:064x}" for index in range(205)]
    state = SimpleNamespace(effect_rows=lambda _kind: iter({"effect_id": identity} for identity in identities))
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: pytest.fail("Private reader must stay loopback"))
    ) as client:
        observed = list(value._delivery_receipts(state, client))
    assert [len(batch) for batch in requests] == [100, 100, 5]
    assert [receipt["effect_id"] for receipt in observed] == identities


@pytest.mark.parametrize(
    "response", [None, {"receipts": {}}, {"receipts": [], "expected": {}}, {"receipts": [{"effect_id": "a" * 64}] * 2}]
)
def test_malformed_private_observation_envelope_fails_closed(response):
    value = oracle()
    value.outcomes = replace(value.outcomes, observer=DeliveryObserverTarget("", "", transport="private_pipe"))
    value.fresh_journal = SimpleNamespace(delivery_receipts=lambda _batch: response)
    with pytest.raises(EvidenceUnavailable, match="Malformed"):
        list(value._delivery_batch(["a" * 64], None))


def test_missing_private_observer_method_does_not_fall_back_to_http():
    value = oracle()
    value.outcomes = replace(value.outcomes, observer=DeliveryObserverTarget("", "", transport="private_pipe"))
    value.fresh_journal = SimpleNamespace()
    with pytest.raises(EvidenceUnavailable):
        list(value._delivery_batch(["a" * 64], None))


def owner(kind, identity):
    return {"kind": kind, "uid": identity, "controller": True}


def pod(name, identity, owner_kind, owner_id, *, ready=True):
    return {
        "metadata": {"name": name, "uid": identity, "ownerReferences": [owner(owner_kind, owner_id)]},
        "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
    }


def test_stateful_replica_resolution_uses_owned_ready_pods_and_all_declared_members(monkeypatch):
    target = HTTPServiceTarget("region-a", "platform-a", "search", resource_kind="statefulset", expected_replicas=2)
    resource = {
        "metadata": {"uid": "stateful-uid", "generation": 2},
        "spec": {"replicas": 2, "selector": {"matchLabels": {"app": "search"}}},
        "status": {"observedGeneration": 2},
    }
    pods = [
        pod("search-1", "pod-1", "StatefulSet", "stateful-uid"),
        pod("search-0", "pod-0", "StatefulSet", "stateful-uid"),
        pod("unrelated", "pod-other", "StatefulSet", "unrelated"),
    ]

    def read(_namespace, args, **_kwargs):
        return {"items": pods} if args[0] == "pods" else resource

    monkeypatch.setattr("sregym.conductor.oracles.regional_database_recovery._kube_json", read)
    observed = resolve_http_replicas(target, deadline=float("inf"))
    assert [value.service for value, _uid in observed] == ["search-0", "search-1"]
    assert all(value.resource_kind == "pod" for value, _uid in observed)
    pods[0]["status"]["conditions"][0]["status"] = "False"
    with pytest.raises(StateMismatch, match="regional_replica_inventory_mismatch"):
        resolve_http_replicas(target, deadline=float("inf"))


def test_deployment_replica_resolution_requires_the_deployment_owner_chain(monkeypatch):
    target = HTTPServiceTarget("region-a", "platform-a", "api", resource_kind="deployment", expected_replicas=2)
    resource = {
        "metadata": {"uid": "deployment-uid", "generation": 1},
        "spec": {"replicas": 2, "selector": {"matchLabels": {"app": "api"}}},
        "status": {"observedGeneration": 1},
    }
    sets = [{"metadata": {"uid": "owned-rs", "ownerReferences": [owner("Deployment", "deployment-uid")]}}]
    pods = [pod("api-a", "pod-a", "ReplicaSet", "owned-rs"), pod("api-b", "pod-b", "ReplicaSet", "owned-rs")]

    def read(_namespace, args, **_kwargs):
        return {"items": pods} if args[0] == "pods" else {"items": sets} if args[0] == "replicasets" else resource

    monkeypatch.setattr("sregym.conductor.oracles.regional_database_recovery._kube_json", read)
    assert len(resolve_http_replicas(target, deadline=float("inf"))) == 2
    sets[0]["metadata"]["ownerReferences"][0]["uid"] = "other-deployment"
    with pytest.raises(StateMismatch, match="regional_replica_inventory_mismatch"):
        resolve_http_replicas(target, deadline=float("inf"))


def test_pod_descriptor_follows_the_live_uid_and_cannot_hide_multiple_replicas(monkeypatch):
    target = HTTPServiceTarget("region-a", "platform-a", "search-0", resource_kind="pod")
    state = pod("search-0", "old-uid", "StatefulSet", "search")
    monkeypatch.setattr(
        "sregym.conductor.oracles.regional_database_recovery._kube_json", lambda *_args, **_kwargs: state
    )
    assert resolve_http_replicas(target, deadline=float("inf"))[0][1] == "old-uid"
    state["metadata"]["uid"] = "new-uid"
    assert resolve_http_replicas(target, deadline=float("inf"))[0][1] == "new-uid"
    with pytest.raises(ValueError, match="shared service"):
        HTTPServiceTarget("region-a", "platform-a", "search", expected_replicas=2)
    with pytest.raises(ValueError, match="exact declared"):
        HTTPServiceTarget("region-a", "platform-a", "api", resource_kind="deployment")


@pytest.mark.parametrize(
    "factory",
    [
        lambda: SQLTarget("group-a", "region-a", "platform-a", "../db", "codehub", "observer", "x" * 32),
        lambda: HTTPServiceTarget("region-a", "platform-a", "api", True),
        lambda: DeliveryObserverTarget("http://user:pass@host/receipts", "x" * 32),
        lambda: GitRefExpectation("refs/heads/../../repair", "a" * 40),
        lambda: GitFileExpectation("a" * 40, "../private.json", "b" * 64),
        lambda: WebhookDestination(uid(900), "http://receiver", ("not-an-operation",)),
    ],
)
def test_endpoint_and_source_contracts_cannot_hide_unscoped_paths_or_bad_types(factory):
    with pytest.raises(ValueError):
        factory()


def fresh_request():
    return {
        "event_id": uid(40),
        "tenant_id": uid(100),
        "entity_id": uid(300),
        "project_id": uid(200),
        "client_revision": 1,
        "kind": "issue.create",
        "payload": {"title": "Delivery timeout", "state": "open"},
    }


def fresh_ack(body):
    return body | {
        "actor_id": uid(1),
        "record_key": f"{body['tenant_id']}/{body['entity_id']}",
        "operation_sha256": digest(body | {"actor_id": uid(1)}),
    }


class ReceiptOwner:
    """An in-memory protocol spy, never a substitute for live SQL grading."""

    def __init__(self):
        self.calls, self.pending, self.acknowledged = [], {}, {}

    def begin_epoch(self):
        self.calls.append(("begin",))
        return 7

    def request(self, epoch, group, operation, effects, provenance):
        self.calls.append(("request", epoch, group, operation, effects, provenance))
        self.pending[operation["event_id"]] = operation
        return True

    def acknowledge(self, epoch, event_id, url, latency_ms, status, acknowledgment):
        assert event_id in self.pending
        self.calls.append(("ack", epoch, event_id, url, latency_ms, status, acknowledgment))
        self.acknowledged[event_id] = acknowledgment
        del self.pending[event_id]
        return True

    def reject(self, epoch, event_id, url, latency_ms, status):
        self.calls.append(("reject", epoch, event_id, url, latency_ms, status))
        del self.pending[event_id]
        return True

    def close_epoch(self, epoch):
        self.calls.append(("close", epoch))
        return not self.pending


def test_fresh_requests_and_exact_receipts_are_persisted_before_the_next_business_check():
    value, request, owner = oracle(), fresh_request(), ReceiptOwner()
    value.fresh_journal = owner
    challenge = value.outcomes.fresh_challenges[0]
    value._journal_request(7, challenge, request)

    def handle(message):
        assert owner.calls[-1][0] == "request"
        assert request["event_id"] in owner.pending
        return httpx.Response(201, json=fresh_ack(request))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        response = value._fresh_post(
            client,
            "http://api/v1/operations",
            request,
            challenge.token,
            float("inf"),
            journal_epoch=7,
            actor_id=challenge.actor_id,
        )
    assert owner.acknowledged[request["event_id"]] == response
    assert [entry[0] for entry in owner.calls] == ["request", "ack"]
    assert owner.calls[0][3] == request | {"actor_id": challenge.actor_id}
    assert {effect["effect_kind"] for effect in owner.calls[0][4]} == {"search", "delivery"}
    assert owner.calls[0][5] is None
    assert owner.calls[1][4] >= 0 and owner.calls[1][5] == 201


def test_malformed_acknowledgment_cannot_be_saved_as_a_committed_fresh_write():
    value, request, owner = oracle(), fresh_request(), ReceiptOwner()
    value.fresh_journal = owner
    challenge = value.outcomes.fresh_challenges[0]
    value._journal_request(7, challenge, request)
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _message: httpx.Response(200, content=b"invalid JSON"))
        ) as client,
        pytest.raises(StateMismatch, match="fresh_commit_acknowledgment_mismatch"),
    ):
        value._fresh_post(
            client,
            "http://api/v1/operations",
            request,
            challenge.token,
            float("inf"),
            journal_epoch=7,
            actor_id=challenge.actor_id,
        )
    assert not owner.acknowledged and request["event_id"] in owner.pending


def test_partial_fresh_probe_keeps_prior_acknowledgments_and_closes_resolved_requests():
    value, owner, requests = oracle(), ReceiptOwner(), []
    value.fresh_journal = owner

    def handle(message):
        body = json.loads(message.content)
        requests.append(body)
        return httpx.Response(200, json=fresh_ack(body)) if len(requests) == 1 else httpx.Response(403)

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(StateMismatch, match="business_endpoint_unavailable"),
    ):
        value._fresh_api(client, ("http://api-a", "http://api-b"), ("http://git",), float("inf"))
    assert [entry[0] for entry in owner.calls] == ["begin", "request", "ack", "request", "reject", "close"]
    assert owner.acknowledged == {requests[0]["event_id"]: fresh_ack(requests[0])}
    assert not owner.pending


def test_partial_probe_with_unknown_io_preserves_prior_ack_and_blocks_an_incomplete_epoch(monkeypatch):
    value, owner, requests = oracle(), ReceiptOwner(), []
    value.fresh_journal = owner

    def handle(message):
        body = json.loads(message.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(200, json=fresh_ack(body))
        raise httpx.ReadTimeout("no response", request=message)

    monkeypatch.setattr("sregym.conductor.oracles.regional_database_recovery.time.sleep", lambda _seconds: None)
    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(EvidenceUnavailable, match="unresolved"),
    ):
        value._fresh_api(client, ("http://api-a", "http://api-b"), ("http://git",), float("inf"))
    assert owner.acknowledged == {requests[0]["event_id"]: fresh_ack(requests[0])}
    assert requests[1:] == [requests[1]] * 3
    assert owner.pending == {requests[1]["event_id"]: requests[1] | {"actor_id": uid(1)}}
    assert owner.calls[-1] == ("close", 7)


@pytest.mark.parametrize(
    "change",
    [
        lambda response: response.update(event_id=uid(41)),
        lambda response: response.update(actor_id=uid(2)),
        lambda response: response.update(payload={"title": "Done", "state": "closed"}),
        lambda response: response.update(operation_sha256="b" * 64),
    ],
)
def test_a_200_status_without_the_exact_committed_operation_is_not_acknowledged(change):
    request = fresh_request()
    response = fresh_ack(request)
    change(response)
    with pytest.raises(StateMismatch, match="fresh_commit_acknowledgment_mismatch"):
        RegionalDatabaseRecoveryOracle._validate_fresh_ack(request, uid(1), response)


def test_transient_retry_preserves_operation_identity(monkeypatch):
    value, request, requests = oracle(), fresh_request(), []

    def handle(message):
        requests.append(json.loads(message.content))
        if len(requests) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=fresh_ack(request))

    monkeypatch.setattr("sregym.conductor.oracles.regional_database_recovery.time.sleep", lambda _seconds: None)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        response = value._fresh_post(client, "http://api/v1/operations", request, "customer-token", float("inf"))
    value._validate_fresh_ack(request, uid(1), response)
    assert requests == [request, request]


def test_unknown_fresh_outcome_remains_unresolved_after_bounded_retries(monkeypatch):
    value, request, requests = oracle(), fresh_request(), []

    def handle(message):
        requests.append(json.loads(message.content))
        raise httpx.ReadTimeout("no response", request=message)

    monkeypatch.setattr("sregym.conductor.oracles.regional_database_recovery.time.sleep", lambda _seconds: None)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client, pytest.raises(TimeoutError, match="unresolved"):
        value._fresh_post(client, "http://api/v1/operations", request, "customer-token", float("inf"))
    assert requests == [request, request, request]


def test_port_forward_is_terminated_when_the_consumer_raises(monkeypatch):
    class Process:
        stopped = False
        killed = False

        def poll(self):
            return None

        def terminate(self):
            self.stopped = True

        def wait(self, timeout):
            if not self.killed:
                raise subprocess.TimeoutExpired("owned", timeout)
            return 0

        def kill(self):
            self.killed = True

    process = Process()
    monkeypatch.setattr(
        "sregym.conductor.oracles.regional_database_recovery.subprocess.Popen", lambda *_args, **_kwargs: process
    )
    monkeypatch.setattr(
        "sregym.conductor.oracles.regional_database_recovery.socket.create_connection",
        lambda *_args, **_kwargs: SimpleNamespace(__enter__=lambda _self: None),
    )

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "sregym.conductor.oracles.regional_database_recovery.socket.create_connection",
        lambda *_args, **_kwargs: Connection(),
    )
    with (
        pytest.raises(RuntimeError, match="consumer"),
        port_forward("platform-a", "db-writer", 3306, deadline=float("inf")),
    ):
        raise RuntimeError("consumer failed")
    assert process.stopped and process.killed


def test_artifact_rows_without_real_content_cannot_pass():
    value = oracle()
    build = {"project_id": uid(200), "commit_sha": "a" * 40, "artifact_sha256": "c" * 64, "file_count": 1}
    state = SimpleNamespace(build_rows=lambda: iter([build]))
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=b"fake artifact"))
        ) as client,
        pytest.raises(StateMismatch, match="artifact_content_mismatch"),
    ):
        value._check_artifacts(state, client, ("http://api",))


def test_real_bundle_is_checked_against_independent_source_and_manifest_hashes():
    value = oracle()
    source = b"def answer():\n    return 42\n"
    source_hash = hashlib.sha256(source).hexdigest()
    manifest = {
        "project_id": uid(200),
        "commit_sha": "a" * 40,
        "builder": "python-validate-bundle-v1",
        "files": {"app.py": source_hash},
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr("app.py", source)
        bundle.writestr("CODEHUB-BUILD.json", canonical(manifest))
    content, checksum = output.getvalue(), hashlib.sha256(output.getvalue()).hexdigest()
    project = replace(
        value.outcomes.projects[0],
        files=(GitFileExpectation("a" * 40, "app.py", source_hash),),
        bundles=(GitBundleExpectation("a" * 40, checksum),),
    )
    state = SimpleNamespace(
        build_rows=lambda: iter(
            [{"project_id": uid(200), "commit_sha": "a" * 40, "artifact_sha256": checksum, "file_count": 1}]
        )
    )
    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=content))) as client:
        value._check_artifacts(state, client, ("http://api",), project_expectations=(project,))
    with (
        httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=content))) as client,
        pytest.raises(StateMismatch, match="build_artifact_identity_mismatch"),
    ):
        value._check_artifacts(
            state,
            client,
            ("http://api",),
            project_expectations=(replace(project, bundles=(GitBundleExpectation("a" * 40, "d" * 64),)),),
        )


@pytest.mark.skipif(shutil.which("git") is None, reason="The local Git executable is required")
def test_fresh_git_flow_creates_and_pushes_real_objects_with_private_new_content(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, timeout=10
        ).stdout

    git("init", "--quiet", str(source))
    (source / "app.py").write_bytes(b"def answer():\n    return 42\n")
    git("-C", str(source), "add", "app.py")
    git(
        "-C",
        str(source),
        "-c",
        "user.name=Release automation",
        "-c",
        "user.email=automation@example.test",
        "commit",
        "--quiet",
        "-m",
        "Initial application",
    )
    parent = git("-C", str(source), "rev-parse", "HEAD").decode().strip()
    value = oracle()
    project = replace(
        value.outcomes.projects[0],
        refs=(GitRefExpectation("refs/heads/main", parent),),
        files=(GitFileExpectation(parent, "app.py", hashlib.sha256((source / "app.py").read_bytes()).hexdigest()),),
        bundles=(GitBundleExpectation(parent, "c" * 64),),
    )
    value.outcomes = replace(value.outcomes, projects=(project,))
    value.fresh_journal = ReceiptOwner()
    real = value._git

    def local_transport(args, **kwargs):
        if args[:3] == ["clone", "--quiet", "--mirror"]:
            return git("clone", "--quiet", "--mirror", str(source), args[-1])
        if "push" in args:
            assert value.fresh_journal.calls[-1][0] == "request"
            operation = value.fresh_journal.calls[-1][3]
            assert operation["kind"] == "repository.push"
            assert operation["payload"]["commit_sha"] in args[-1]
            return git(*args)
        return real(args, **kwargs)

    monkeypatch.setattr(value, "_git", local_transport)
    request, expectation = value._fresh_git(
        value.outcomes.fresh_challenges[0],
        "http://repository",
        float("inf"),
        journal_epoch=7,
    )
    assert request["kind"] == "repository.push"
    assert request["payload"]["commit_sha"] != parent
    assert request["payload"]["ref"] == expectation.refs[0].ref
    actual = git("-C", str(source), "rev-parse", "--verify", request["payload"]["ref"]).decode().strip()
    assert actual == request["payload"]["commit_sha"]
    git("-C", str(source), "fsck", "--strict", "--no-dangling")
    added = next(item for item in expectation.files if item.path != "app.py")
    content = git("-C", str(source), "show", f"{actual}:{added.path}")
    assert hashlib.sha256(content).hexdigest() == added.sha256
    configuration = json.loads(content)
    assert configuration["enabled"] is True and len(configuration["release"]) >= 32
    assert expectation.bundles[0].commit_sha == actual
    provenance = value.fresh_journal.calls[-1][5]
    assert provenance["project_id"] == project.project_id
    assert provenance["refs"] == [{"ref": request["payload"]["ref"], "commit_sha": actual}]
    assert provenance["bundles"] == [{"commit_sha": actual, "sha256": expectation.bundles[0].sha256}]
    assert {item["path"]: item["sha256"] for item in provenance["files"]} == {
        item.path: item.sha256 for item in expectation.files
    }
    assert value.outcomes.projects == (project,)  # Existing private expectations were never rebased.


@pytest.mark.parametrize("budget", [0, 1, True, "1073741824", 9 * 1024**3])
def test_regional_scratch_admission_rejects_untrusted_or_insufficient_budgets(budget):
    with pytest.raises(ValueError, match="scratch budget"):
        RegionalDatabaseRecoveryOracle(SimpleNamespace(), verification_scratch_bytes=budget)


def test_regional_sql_spools_share_the_trusted_aggregate_budget(monkeypatch):
    import sregym.conductor.oracles.regional_database_recovery as module

    value = oracle()
    monkeypatch.delenv("SREGYM_VERIFIER_CONTAINER", raising=False)
    cut = value.cuts[0]
    with value._protected_state(cut) as state:
        assert state.spool_bytes * 3 + 128 * 1024**2 <= value.verification_scratch_bytes
        assert state.db.execute("PRAGMA max_page_count").fetchone()[0] * 4096 <= state.spool_bytes
    calls = []
    monkeypatch.setenv("SREGYM_VERIFIER_CONTAINER", "1")
    monkeypatch.setattr(module, "ProtectedState", lambda cut, **kwargs: calls.append(kwargs))
    value._protected_state(cut)
    assert calls[0]["scratch_dir"] == "/scratch"


def test_spool_exhaustion_is_a_harness_failure_not_a_solver_negative_control(monkeypatch):
    from sregym.conductor.oracles.codehub_state import StateMismatch

    value = oracle()
    value.capture_baseline()
    value.fresh_journal = object()
    monkeypatch.setenv("SREGYM_VERIFIER_CONTAINER", "1")
    monkeypatch.setattr(value, "_http_inventory", lambda deadline: {"api": (), "search": (), "repository": ()})

    def exhausted(*args, **kwargs):
        raise StateMismatch("verification_spool_capacity_exceeded")

    monkeypatch.setattr(value, "_check_sql", exhausted)
    verdict = value.evaluate()
    assert verdict["success"] is False
    assert verdict["reason"] == "recovery_verification_capacity_unavailable"
    assert verdict["failure_class"] == "harness_error"


@contextmanager
def projection_fixture(*tenant_counts):
    """Real SQLite history anchored to independently constructed closed receipts."""
    operations, latest, rows, sequence = [], [], {}, 0
    for tenant, count in tenant_counts:
        for number in range(count):
            entity = uid(1000 + sequence)
            project = uid(200 + tenant - 100)
            for revision in (1, 2):
                op = AcceptedOperation(
                    uid(10000 + 2 * sequence + revision),
                    uid(tenant),
                    entity,
                    project,
                    revision,
                    "issue.create" if revision == 1 else "issue.update",
                    canonical({"title": f"Delivery {number}", "body": f"Details {revision}", "state": "open"}),
                    uid(1),
                )
                operations.append(op.row())
            latest.append(op.row())
            rows[(uid(tenant), entity)] = {
                "tenant_id": uid(tenant),
                "id": entity,
                "project_id": project,
                "revision": 2,
                "entity_type": "issue",
                "document": {"title": f"Delivery {number}", "body": "Details 2", "state": "open", "author_id": uid(1)},
            }
            sequence += 1
    latest.sort(key=lambda row: (row["tenant_id"], row["entity_id"]))

    def receipt_hash(values):
        checksum = hashlib.sha256()
        for value in values:
            checksum.update(canonical(value).encode() + b"\n")
        return checksum.hexdigest()

    cut = ProtectedReceiptCut(
        "group-a",
        (0,),
        len(operations),
        tuple(f"{row['tenant_id']}/{row['entity_id']}" for row in latest),
        receipt_hash(sorted(operations, key=lambda row: row["event_id"])),
        receipt_hash(latest),
    )
    with ProtectedState(cut) as state:
        state.load_journal(operations)
        yield state, rows


def lookup_items(message, rows, *, search):
    assert message.method == "POST"
    body = json.loads(message.content)
    assert set(body) == {"ids"}
    assert 1 <= len(body["ids"]) <= 128
    assert len(body["ids"]) == len(set(body["ids"]))
    assert len(message.content) <= 8 * 1024
    tenant = message.url.path.split("/")[3]
    result = [json.loads(canonical(rows[(tenant, identity)])) for identity in body["ids"]]
    if search:
        for row in result:
            del row["entity_type"]
            row["kind"] = "issue.update"
            del row["document"]["author_id"]
    return result


@pytest.mark.parametrize("search", [False, True])
def test_exact_projection_batches_cover_all_tenants_records_and_concrete_replicas(search):
    value, requests, workers = oracle(), [], set()
    other = FreshAPIChallenge("group-a", uid(101), uid(201), uid(1), "other-customer-" + "t" * 32)
    value.outcomes = replace(value.outcomes, fresh_challenges=value.outcomes.fresh_challenges + (other,))
    origins = tuple(f"http://replica-{index}" for index in range(11))
    main_thread = threading.get_ident()

    def handle(message):
        workers.add(threading.get_ident())
        items = lookup_items(message, rows, search=search)
        requests.append((message.url.host, message.url.path, tuple(item["id"] for item in items)))
        expected_token = (
            value.outcomes.service_token
            if search
            else (other.token if items[0]["tenant_id"] == other.tenant_id else value.outcomes.fresh_challenges[0].token)
        )
        assert message.headers["Authorization"] == f"Bearer {expected_token}"
        assert message.url.path.endswith("/entities/lookup")
        assert message.url.path.startswith("/internal/" if search else "/v1/")
        return httpx.Response(200, json={"items": list(reversed(items))})

    with (
        projection_fixture((100, 257), (101, 129)) as (state, rows),
        httpx.Client(transport=httpx.MockTransport(handle), headers={"Authorization": "unchanged"}) as client,
    ):
        value._check_projections(state, client, origins, search=search, deadline=time.monotonic() + 30)
        assert client.headers["Authorization"] == "unchanged"
    assert main_thread not in workers
    for origin in origins:
        selected = [entry for entry in requests if f"http://{entry[0]}" == origin]
        assert [len(entry[2]) for entry in selected] == [128, 128, 1, 128, 1]
        assert {(entry[1].split("/")[3], identity) for entry in selected for identity in entry[2]} == set(rows)


@pytest.mark.parametrize("search", [False, True])
@pytest.mark.parametrize(
    "corruption",
    [
        "missing",
        "duplicate",
        "extra",
        "tenant",
        "revision",
        "higher_revision",
        "boolean_revision",
        "float_revision",
        "content",
        "type",
        "field",
        "project",
        "document_type",
        "item_type",
    ],
)
def test_every_projection_replica_rejects_incomplete_or_fabricated_last_batch(search, corruption):
    value = oracle()
    calls = []

    def handle(message):
        items = lookup_items(message, rows, search=search)
        calls.append((message.url.host, len(items)))
        if message.url.host == "replica-b" and len(items) == 1:
            row = items[0]
            if corruption == "missing":
                items.clear()
            elif corruption == "duplicate":
                items.append(row.copy())
            elif corruption == "extra":
                row["id"] = uid(99999)
            elif corruption == "tenant":
                row["tenant_id"] = uid(101)
            elif corruption == "revision":
                row["revision"] = 1
            elif corruption == "higher_revision":
                row["revision"] = 3
            elif corruption == "boolean_revision":
                row["revision"] = True
            elif corruption == "float_revision":
                row["revision"] = 2.0
            elif corruption == "content":
                row["document"]["body"] = "Repaired-looking fabricated content"
            elif corruption == "type":
                row["kind" if search else "entity_type"] = "project.create" if search else "project"
            elif corruption == "field":
                row["observed_total"] = 129
            elif corruption == "project":
                row["project_id"] = uid(201)
            elif corruption == "document_type":
                row["document"] = []
            else:
                items[0] = []
        return httpx.Response(200, json={"items": items})

    reason = "search_projection_mismatch" if search else "regional_api_projection_mismatch"
    with (
        projection_fixture((100, 129)) as (state, rows),
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(StateMismatch, match=reason),
    ):
        value._check_projections(
            state, client, ("http://replica-a", "http://replica-b"), search=search, deadline=time.monotonic() + 10
        )
    assert ("replica-a", 128) in calls and ("replica-b", 128) in calls
    assert ("replica-b", 1) in calls


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"null",
        b"[]",
        b'{"items":{}}',
        b'{"items":[],"total":0}',
        b'{"items":[],"items":[]}',
        b'{"items":[NaN]}',
    ],
)
def test_projection_batch_envelope_is_strict_and_malformed_public_facts_are_solver_mismatches(content):
    with (
        projection_fixture((100, 1)) as (state, _rows),
        httpx.Client(transport=httpx.MockTransport(lambda _message: httpx.Response(200, content=content))) as client,
        pytest.raises(StateMismatch, match="regional_api_projection_mismatch"),
    ):
        oracle()._check_api_current(state, client, ("http://api",), deadline=time.monotonic() + 10)


@pytest.mark.parametrize("corruption", ["same_count_duplicate", "duplicate_revision_field", "duplicate_document_field"])
def test_projection_batches_reject_duplicate_identities_and_duplicate_json_even_if_last_value_is_correct(corruption):
    def handle(message):
        items = lookup_items(message, rows, search=False)
        if corruption == "same_count_duplicate":
            items[1] = items[0]
        content = canonical({"items": items})
        if corruption == "duplicate_revision_field":
            content = content.replace('"revision":2', '"revision":1,"revision":2', 1)
        elif corruption == "duplicate_document_field":
            content = content.replace('"body":"Details 2"', '"body":"fabricated","body":"Details 2"', 1)
        return httpx.Response(200, content=content.encode())

    with (
        projection_fixture((100, 2)) as (state, rows),
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(StateMismatch, match="regional_api_projection_mismatch"),
    ):
        oracle()._check_api_current(state, client, ("http://api",))


def test_projection_waves_never_spawn_more_than_eight_replica_requests(monkeypatch):
    value, active, peak, completed, lock = oracle(), 0, 0, [], threading.Lock()
    first_wave = threading.Barrier(8)
    observations = []

    def check(_client, origin, _tenant, expected, **_kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            observations.append((origin, tuple(completed)))
        with pytest.raises(TypeError):
            expected[uid(999)] = "fabricated"
        if int(origin.rsplit("-", 1)[-1]) < 8:
            first_wave.wait(timeout=5)
        with lock:
            completed.append(origin)
            active -= 1

    monkeypatch.setattr(value, "_check_projection_batch", check)
    origins = tuple(f"http://replica-{index}" for index in range(17))
    with projection_fixture((100, 1)) as (state, _rows):
        value._check_api_current(state, None, origins, deadline=time.monotonic() + 10)
    assert peak == 8 and set(completed) == set(origins)
    assert all(len(prior) >= 8 for origin, prior in observations if int(origin.rsplit("-", 1)[-1]) >= 8)
    assert all(len(prior) >= 16 for origin, prior in observations if int(origin.rsplit("-", 1)[-1]) >= 16)


def test_projection_failure_joins_its_bounded_wave_without_starting_more_replica_requests(monkeypatch):
    value, calls, finished = oracle(), [], []
    barrier = threading.Barrier(8)

    def check(_client, origin, _tenant, _expected, **_kwargs):
        calls.append(origin)
        barrier.wait(timeout=5)
        finished.append(origin)
        raise TimeoutError("Business observation deadline exceeded")

    monkeypatch.setattr(value, "_check_projection_batch", check)
    with projection_fixture((100, 1)) as (state, _rows), pytest.raises(TimeoutError):
        value._check_api_current(
            state, None, tuple(f"http://replica-{index}" for index in range(20)), deadline=time.monotonic() + 10
        )
    assert set(calls) == {f"http://replica-{index}" for index in range(8)}
    assert set(finished) == set(calls)


def test_protected_tenant_without_customer_credentials_is_not_read_with_service_credentials():
    with (
        projection_fixture((101, 1)) as (state, _rows),
        httpx.Client(
            transport=httpx.MockTransport(lambda _message: pytest.fail("Customer credential inventory is required"))
        ) as client,
        pytest.raises(ValueError, match="customer read credentials"),
    ):
        oracle()._check_api_current(state, client, ("http://api",))


def test_empty_concrete_replica_inventory_cannot_skip_exact_projections():
    with pytest.raises(ValueError, match="actual replica"):
        oracle()._check_api_current(SimpleNamespace(), None, ())


def test_expired_projection_deadline_does_not_iterate_sqlite_or_start_io():
    state = SimpleNamespace(latest=lambda: pytest.fail("Expired deadline must not start another SQLite stream"))
    with pytest.raises(TimeoutError, match="deadline"):
        oracle()._check_api_current(state, None, ("http://api",), deadline=time.monotonic() - 1)


def test_projection_deadline_is_checked_while_streaming_and_bounds_http_timeout(monkeypatch):
    import sregym.conductor.oracles.regional_database_recovery as module

    clock, timeouts = [0.0], []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    class SlowBody(httpx.SyncByteStream):
        def __iter__(self):
            yield b"x"
            clock[0] = 1.0
            yield b"x"

    def handle(message):
        timeouts.append(message.extensions["timeout"])
        return httpx.Response(200, stream=SlowBody())

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(TimeoutError, match="deadline"),
    ):
        oracle()._body(client, "POST", "http://api", deadline=1.0)
    assert timeouts == [{"connect": 1.0, "read": 1.0, "write": 1.0, "pool": 1.0}]


def test_projection_lookup_body_bound_does_not_expand_legacy_http_reads(monkeypatch):
    import sregym.conductor.oracles.regional_database_recovery as module

    limits = []
    value = oracle()

    def body(_client, _method, _url, *, maximum, **_kwargs):
        limits.append(maximum)
        return b'{"items":[]}'

    monkeypatch.setattr(value, "_body", body)
    with projection_fixture((100, 1)) as (state, _rows), pytest.raises(StateMismatch):
        value._check_api_current(state, None, ("http://api",))
    assert limits == [40 * 1024**2]
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _message: httpx.Response(200, content=b"oversized"))
        ) as client,
        pytest.raises(StateMismatch, match="business_response_oversized"),
    ):
        module.RegionalDatabaseRecoveryOracle._body(client, "GET", "http://api", maximum=4)


def observation_loop_fixture(monkeypatch, *, failed_last_member=False, two_groups=False):
    """Loop protocol spies expose consumption; they provide no live SQL proof."""
    import sregym.conductor.oracles.regional_database_recovery as module

    value, clock, iterations = oracle(), [0.0], []
    if two_groups:
        value.cuts += (replace(value.cuts[0], group="group-b"),)
        value.effect_cuts += (replace(value.effect_cuts[0], group="group-b"),)
        value.databases += tuple(
            replace(target, group="group-b", service=f"b-{target.service}") for target in value.databases
        )
    value.capture_baseline()
    value.fresh_journal = object()
    value.stable_seconds, value.deadline_seconds = 2, 20
    calls = {name: [] for name in ("api", "search", "delivery", "artifacts", "git", "sql")}
    fresh_cut = replace(
        value.cuts[0], closed_epochs=(1,), operations=2, journal_sha256="d" * 64, current_sha256="e" * 64
    )
    fresh = ((fresh_cut, EffectReceiptCut("group-a", 1, "f" * 64)),)
    monkeypatch.setenv("SREGYM_VERIFIER_CONTAINER", "1")
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    def sleep(seconds):
        clock[0] += 20 if failed_last_member else min(1, seconds)

    monkeypatch.setattr(module.time, "sleep", sleep)

    def inventory(_deadline):
        iterations.append(len(iterations) + 1)
        uid_prefix = "new" if len(iterations) >= 3 else "old"
        return {
            role: ((value.outcomes.api_targets[0], f"{role}-{uid_prefix}"),) for role in ("api", "search", "repository")
        }

    @contextmanager
    def forward(*_args, **_kwargs):
        yield 12345

    monkeypatch.setattr(value, "_http_inventory", inventory)
    monkeypatch.setattr(module, "port_forward", forward)

    def state(cut):
        return SimpleNamespace(
            cut=cut,
            db=SimpleNamespace(execute=lambda _query: SimpleNamespace(fetchone=lambda: (1,))),
            check_delivery_receipts=lambda receipts: calls["delivery"].append(
                (len(iterations), cut.current_sha256, tuple(receipts))
            ),
        )

    def sql(_deadline, *, extra):
        for index, target in enumerate(value.databases):
            calls["sql"].append((len(iterations), index, bool(extra)))
            if failed_last_member and index == len(value.databases) - 1:
                raise StateMismatch("accepted_history_mismatch")
            cut = next(cut for cut in value.cuts if cut.group == target.group)
            yield target, state(cut)
            for cut, _effects in extra:
                if cut.group == target.group:
                    yield target, state(cut)

    monkeypatch.setattr(value, "_check_sql", sql)
    for name, method in (("api", "_check_api_current"), ("search", "_check_search"), ("artifacts", "_check_artifacts")):
        monkeypatch.setattr(
            value,
            method,
            lambda state, *_args, _name=name, **_kwargs: calls[_name].append(
                (len(iterations), state.cut.current_sha256)
            ),
        )
    monkeypatch.setattr(value, "_delivery_receipts", lambda *_args: iter(()))
    monkeypatch.setattr(value, "_check_git", lambda *_args, **_kwargs: calls["git"].append(len(iterations)))
    monkeypatch.setattr(value, "_fresh_api", lambda *_args: fresh)
    return value, calls, iterations, fresh_cut


def test_business_observations_are_once_per_cut_while_all_sql_members_and_stability_observations_repeat(monkeypatch):
    value, calls, iterations, fresh_cut = observation_loop_fixture(monkeypatch)
    assert value.evaluate() == {"success": True}
    # The third observation's UID turnover resets the window, delaying pass.
    assert iterations == [1, 2, 3, 4, 5]
    for iteration in iterations:
        assert [(index, fresh) for observed, index, fresh in calls["sql"] if observed == iteration] == [
            (index, iteration > 1) for index in range(4)
        ]
        expected_cuts = [value.cuts[0].current_sha256] + ([fresh_cut.current_sha256] if iteration > 1 else [])
        for role in ("api", "search", "artifacts"):
            assert [checksum for observed, checksum in calls[role] if observed == iteration] == expected_cuts
        assert [
            checksum for observed, checksum, _receipts in calls["delivery"] if observed == iteration
        ] == expected_cuts
    assert calls["git"] == iterations


def test_last_sql_member_mismatch_cannot_pass_after_prior_exact_business_reads(monkeypatch):
    value, calls, iterations, _fresh = observation_loop_fixture(monkeypatch, failed_last_member=True)
    verdict = value.evaluate()
    assert verdict["success"] is False
    assert verdict["reason"] == "recovery_state_mismatch"
    assert verdict["detail"]["check"] == "accepted_history_mismatch"
    assert calls["api"] == [(1, value.cuts[0].current_sha256)]
    assert len(calls["sql"]) == 4 and iterations == [1]
    assert not calls["git"]


def test_distinct_database_groups_never_share_a_business_observation_cache(monkeypatch):
    value, calls, iterations, _fresh = observation_loop_fixture(monkeypatch, two_groups=True)
    assert value.evaluate() == {"success": True}
    assert iterations == [1, 2, 3, 4, 5]
    for iteration in iterations:
        assert len([entry for entry in calls["sql"] if entry[0] == iteration]) == 8
        for role in ("api", "search", "artifacts", "delivery"):
            assert len([entry for entry in calls[role] if entry[0] == iteration]) == (2 if iteration == 1 else 3)
