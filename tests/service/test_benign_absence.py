import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

import sregym.service.kubectl as kubectl_module
from sregym.service.apps.base import Application
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.helm import Helm
from sregym.service.kubectl import KubeCtl
from sregym.service.mcp_server import MCPServer


class _FakeKubeCtl:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.commands = []

    def exec_command(self, command):
        self.commands.append(command)
        return self.responses.get(command, "")


def test_application_namespace_lookup_treats_empty_output_as_absent():
    namespace = "example"
    lookup = f"kubectl get namespace {namespace} --ignore-not-found -o name"
    fake = _FakeKubeCtl({lookup: "", f"kubectl create namespace {namespace}": f"namespace/{namespace} created\n"})
    app = object.__new__(Application)
    app.namespace = namespace
    app.kubectl = fake
    app.logger = logging.getLogger("test.application")

    app.create_namespace()

    assert fake.commands == [lookup, f"kubectl create namespace {namespace}"]


def test_application_namespace_lookup_keeps_existing_namespace():
    namespace = "example"
    lookup = f"kubectl get namespace {namespace} --ignore-not-found -o name"
    fake = _FakeKubeCtl({lookup: f"namespace/{namespace}\n"})
    app = object.__new__(Application)
    app.namespace = namespace
    app.kubectl = fake
    app.logger = logging.getLogger("test.application")

    app.create_namespace()

    assert fake.commands == [lookup]


def test_social_network_tls_lookup_treats_empty_output_as_absent():
    lookup = "kubectl get secret mongodb-tls -n social-network --ignore-not-found -o name"
    create = (
        "kubectl create secret generic mongodb-tls "
        "--from-file=tls.pem=/tls/tls.pem "
        "--from-file=ca.crt=/tls/ca.crt "
        "-n social-network"
    )
    fake = _FakeKubeCtl({lookup: "", create: "secret/mongodb-tls created\n"})
    app = object.__new__(SocialNetwork)
    app.namespace = "social-network"
    app.local_tls_path = Path("/tls")
    app.kubectl = fake

    app.create_tls_secret()

    assert fake.commands == [lookup, create]


def test_social_network_tls_lookup_keeps_existing_secret():
    lookup = "kubectl get secret mongodb-tls -n social-network --ignore-not-found -o name"
    fake = _FakeKubeCtl({lookup: "secret/mongodb-tls\n"})
    app = object.__new__(SocialNetwork)
    app.namespace = "social-network"
    app.local_tls_path = Path("/tls")
    app.kubectl = fake

    app.create_tls_secret()

    assert fake.commands == [lookup]


@pytest.mark.parametrize(("output", "expected"), [("", False), ("1", True)])
def test_mcp_lookup_ignores_expected_absence(output, expected):
    command = "kubectl get deployment mcp-server -n sregym --ignore-not-found -o jsonpath='{.status.readyReplicas}'"
    fake = _FakeKubeCtl({command: output})
    server = object.__new__(MCPServer)
    server.service_name = "mcp-server"
    server.namespace = "sregym"
    server.kubectl = fake

    assert server._is_running() is expected
    assert fake.commands == [command]


def test_missing_helm_release_is_a_debug_noop(monkeypatch, caplog):
    monkeypatch.setattr(Helm, "_release_status", staticmethod(lambda _release, _namespace: False))

    with caplog.at_level(logging.DEBUG, logger="all.infra.helm"):
        Helm.uninstall(release_name="missing", namespace="example")

    record = next(record for record in caplog.records if "does not exist" in record.message)
    assert record.levelno == logging.DEBUG


def test_helm_release_lookup_failure_remains_an_error(monkeypatch, caplog):
    process = SimpleNamespace(returncode=1, communicate=lambda: (b"", b"cluster unavailable"))
    monkeypatch.setattr("sregym.service.helm.subprocess.Popen", lambda *args, **kwargs: process)

    with caplog.at_level(logging.DEBUG, logger="all.infra.helm"):
        Helm.uninstall(release_name="example", namespace="observe")

    record = next(record for record in caplog.records if "Failed to list Helm releases" in record.message)
    assert record.levelno == logging.ERROR
    assert not any("does not exist" in record.message for record in caplog.records)


def test_missing_namespace_is_debug_but_other_delete_failures_are_errors(caplog):
    kubectl = object.__new__(KubeCtl)

    def raise_status(status):
        def delete_namespace(name):
            raise ApiException(status=status)

        kubectl.core_v1_api = SimpleNamespace(delete_namespace=delete_namespace)

    with caplog.at_level(logging.DEBUG, logger="all.infra.kubectl"):
        raise_status(404)
        kubectl.delete_namespace("missing")
        raise_status(500)
        kubectl.delete_namespace("broken")

    missing_record = next(record for record in caplog.records if record.message == "Namespace 'missing' not found.")
    broken_record = next(
        record for record in caplog.records if record.message.startswith("Error deleting namespace 'broken':")
    )
    assert missing_record.levelno == logging.DEBUG
    assert broken_record.levelno == logging.ERROR


def test_no_matching_cleanup_jobs_is_a_debug_noop(monkeypatch, caplog):
    batch_api = SimpleNamespace(
        list_namespaced_job=lambda namespace, label_selector: SimpleNamespace(items=[]),
    )
    monkeypatch.setattr(kubectl_module.client, "BatchV1Api", lambda: batch_api)
    kubectl = object.__new__(KubeCtl)

    with caplog.at_level(logging.DEBUG, logger="all.infra.kubectl"):
        result = kubectl.delete_job(label="job=workload", namespace="example")

    assert result is True
    record = next(record for record in caplog.records if "No jobs found" in record.message)
    assert record.levelno == logging.DEBUG


def test_missing_cleanup_job_is_a_debug_noop(monkeypatch, caplog):
    def delete_namespaced_job(**_kwargs):
        raise ApiException(status=404)

    batch_api = SimpleNamespace(delete_namespaced_job=delete_namespaced_job)
    monkeypatch.setattr(kubectl_module.client, "BatchV1Api", lambda: batch_api)
    kubectl = object.__new__(KubeCtl)

    with caplog.at_level(logging.DEBUG, logger="all.infra.kubectl"):
        result = kubectl.delete_job(job_name="missing", namespace="example")

    assert result is True
    record = next(record for record in caplog.records if "already deleted" in record.message)
    assert record.levelno == logging.DEBUG
