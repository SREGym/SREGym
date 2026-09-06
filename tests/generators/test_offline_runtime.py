"""Runtime helpers start their existing programs without package downloads."""

import base64
import re
from types import SimpleNamespace
from unittest.mock import Mock

from kubernetes import client

from sregym.conductor.problems.node_clock_drift import NodeClockDriftHotelReservation
from sregym.generators.fault import inject_app
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.generators.fault.inject_kafka import KafkaFaultInjector
from sregym.service.runtime_images import KAFKA_CLIENT_IMAGE, REDIS_CLIENT_IMAGE, TLS_CLIENT_IMAGE


def _application_injector():
    injector = object.__new__(ApplicationFaultInjector)
    injector.namespace = "test-app"
    injector.kubectl = Mock()
    return injector


def _decoded_program(command):
    encoded = re.search(r"b64decode\('([^']+)'\)", command[-1]).group(1)
    return base64.b64decode(encoded).decode()


def test_valkey_job_starts_the_writer_without_pip(monkeypatch):
    api = Mock()
    monkeypatch.setattr(inject_app.client, "BatchV1Api", lambda: api)
    injector = _application_injector()

    injector.inject_valkey_memory_disruption()

    job = api.create_namespaced_job.call_args.kwargs["body"]
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == REDIS_CLIENT_IMAGE
    assert container["command"][:2] == ["python3", "-c"]
    script = _decoded_program(container["command"])
    assert "import redis" in script
    assert "client.set(" in script
    assert "range(10)" in script


def test_kafka_sidecar_starts_the_producer_without_pip():
    injector = _application_injector()
    kafka = client.V1Deployment(
        spec=client.V1DeploymentSpec(
            selector=client.V1LabelSelector(),
            template=client.V1PodTemplateSpec(
                spec=client.V1PodSpec(
                    containers=[
                        client.V1Container(
                            name="kafka",
                            env=[client.V1EnvVar(name="KAFKA_HEAP_OPTS", value="-Xmx400m")],
                            resources=client.V1ResourceRequirements(limits={"memory": "600Mi"}),
                        )
                    ]
                )
            ),
        )
    )
    checkout = SimpleNamespace(spec=SimpleNamespace(template=SimpleNamespace(spec=SimpleNamespace(containers=[]))))
    injector.kubectl.get_deployment.side_effect = [kafka, checkout]
    injector.kubectl.list_pods.return_value = SimpleNamespace(
        items=[
            SimpleNamespace(
                metadata=SimpleNamespace(name="kafka-123"),
                status=SimpleNamespace(container_statuses=[SimpleNamespace(name="kafka", restart_count=1)]),
            )
        ]
    )

    assert injector.inject_kafka_producer_leak() == ["-Xmx400m", "600Mi"]

    container = checkout.spec.template.spec.containers[0]
    assert container.image == KAFKA_CLIENT_IMAGE
    assert container.command[:3] == ["python3", "-u", "-c"]
    script = _decoded_program(container.command)
    assert "from confluent_kafka import Producer" in script
    assert "'10000000'" in script
    assert "range(20)" in script


def test_pipeline_starts_the_script_without_pip():
    injector = object.__new__(KafkaFaultInjector)
    injector.namespace = "test-app"
    injector.kubectl = Mock()

    for name, script in [(injector.PRODUCER_DEPLOYMENT, "producer.py"), (injector.CONSUMER_DEPLOYMENT, "consumer.py")]:
        injector._apply_pipeline_deployment(name, script)
        manifest = injector.kubectl.apps_v1_api.create_namespaced_deployment.call_args.args[1]
        container = manifest["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == KAFKA_CLIENT_IMAGE
        assert container["command"] == ["python", f"/scripts/{script}"]
        assert container["volumeMounts"] == [{"name": "scripts", "mountPath": "/scripts"}]


def test_tls_sidecar_uses_preinstalled_openssl():
    problem = object.__new__(NodeClockDriftHotelReservation)
    problem.namespace = "test-app"
    problem.kubectl = Mock()

    problem._add_tls_health_check_sidecar()

    patch = problem.kubectl.patch_deployment.call_args.args[2]
    container = patch["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == TLS_CLIENT_IMAGE
    assert "apt-get" not in container["args"][0]
    assert "openssl verify -verbose -CAfile /etc/tls-ca/ca.crt /etc/tls-ca/ca.crt" in container["args"][0]
    assert container["readinessProbe"]["exec"]["command"] == ["test", "-f", "/tmp/sidecar-ready"]
