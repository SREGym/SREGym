"""Setup checks must not reuse an old Ready pod from an unfinished rollout."""

import json
from unittest.mock import Mock

import pytest

from sregym.conductor.conductor import Conductor
from sregym.generators.workload.trainticket_locust import TrainTicketLocustWorkloadManager
from sregym.observer.ingress_nginx import IngressNginx
from sregym.observer.otel_collector.otel_collector import OtelCollector
from sregym.service.mcp_server import MCPServer


def response(total=1, updated=1):
    return json.dumps(
        {
            "metadata": {"generation": 2},
            "spec": {"replicas": 1},
            "status": {
                "observedGeneration": 2,
                "replicas": total,
                "updatedReplicas": updated,
                "readyReplicas": 1,
                "availableReplicas": 1,
            },
        }
    )


@pytest.mark.parametrize("klass", [IngressNginx, OtelCollector])
def test_observer_waits_for_the_current_rollout(monkeypatch, klass):
    monkeypatch.setattr("time.sleep", lambda _: None)
    observer = klass()
    observer.run_cmd = Mock(side_effect=[response(total=2), response(updated=0), response()])
    observer._wait_for_ready(timeout=1)
    assert observer.run_cmd.call_count == 3
    assert "-o json" in observer.run_cmd.call_args.args[0]


@pytest.mark.parametrize(
    "data,expected",
    [
        (response(), True),
        (response(total=2), False),
        (response(updated=0), False),
        ("", False),
        ("Error from server (Forbidden)", False),
    ],
)
def test_reused_infrastructure_requires_a_complete_deployment(data, expected):
    for klass, method in [
        (MCPServer, "_is_running"),
        (TrainTicketLocustWorkloadManager, "_is_locust_ready"),
        (Conductor, "_openebs_ready"),
    ]:
        obj = object.__new__(klass)
        obj.namespace = "example"
        obj.service_name = "mcp-server"
        obj.kubectl = Mock()
        obj.kubectl.exec_command.return_value = data
        args = (True,) if klass is Conductor else ()
        assert getattr(obj, method)(*args) is expected
