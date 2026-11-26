import json
from unittest.mock import patch

import pytest

from sregym.observer.otel_collector import otel_collector
from sregym.observer.otel_collector.otel_collector import OtelCollector


def test_otel_collector_deploy_uses_configured_ready_timeout(monkeypatch):
    monkeypatch.setattr(otel_collector, "OTEL_COLLECTOR_READY_TIMEOUT", 1800)
    collector = OtelCollector()
    with (
        patch.object(collector, "_create_jaeger_backend_service") as create_svc,
        patch.object(collector, "run_cmd") as run_cmd,
        patch.object(collector, "_wait_for_ready") as wait_ready,
    ):
        collector.deploy()

    create_svc.assert_called_once_with()
    run_cmd.assert_called_once()
    wait_ready.assert_called_once_with(timeout=1800)


def test_otel_collector_wait_for_ready_raises_after_configured_timeout(monkeypatch):
    monkeypatch.setattr(otel_collector, "OTEL_COLLECTOR_READY_TIMEOUT", 1800)
    collector = OtelCollector()
    ticks = iter([0.0, 100.0, 1801.0])
    monkeypatch.setattr(otel_collector.time, "time", lambda: next(ticks))
    monkeypatch.setattr(otel_collector.time, "sleep", lambda _: None)
    unready = json.dumps({"spec": {"replicas": 1}, "status": {"readyReplicas": 0}})
    with (
        patch.object(collector, "run_cmd", return_value=unready),
        pytest.raises(RuntimeError, match="within 1800s"),
    ):
        collector._wait_for_ready()
