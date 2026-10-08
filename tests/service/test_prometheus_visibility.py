from pathlib import Path

import yaml

from mcp_server import prometheus_server
from sregym.service.agent_visibility_policy import HIDDEN_NAMESPACES, MCP_CONTROL_NAMESPACE, VERIFIER_PROBE_NAMESPACE


class FakeResponse:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


def test_prometheus_tools_hide_chaos_metrics_and_alerts(monkeypatch):
    class FakeClient:
        def __init__(self, _url):
            pass

        def make_request(self, _method, url, **_kwargs):
            if url.endswith("/query"):
                return FakeResponse(
                    {
                        "data": {
                            "resultType": "vector",
                            "result": [
                                {"metric": {"namespace": "chaos-mesh", "pod": "chaos-daemon-123"}, "value": [1, "1"]},
                                {"metric": {"namespace": "khaos", "pod": "helper"}, "value": [1, "1"]},
                                {"metric": {"namespace": "sregym-verifier", "pod": "private-probe"}, "value": [1, "1"]},
                                {
                                    "metric": {"namespace": VERIFIER_PROBE_NAMESPACE, "pod": "node-probe"},
                                    "value": [1, "1"],
                                },
                                {
                                    "metric": {"namespace": MCP_CONTROL_NAMESPACE, "pod": "mcp-server"},
                                    "value": [1, "1"],
                                },
                                {"metric": {"namespace": "sregym", "pod": "legacy-mcp"}, "value": [1, "1"]},
                                {"metric": {"namespace": "astronomy-shop", "pod": "checkout"}, "value": [1, "1"]},
                            ],
                        }
                    }
                )
            return FakeResponse(
                {
                    "data": {
                        "alerts": [
                            {"state": "firing", "labels": {"namespace": "chaos-mesh", "alertname": "PodDown"}},
                            {"state": "firing", "labels": {"namespace": "sregym-verifier", "alertname": "ProbeDown"}},
                            {
                                "state": "firing",
                                "labels": {"namespace": MCP_CONTROL_NAMESPACE, "alertname": "ControlDown"},
                            },
                            {"state": "firing", "labels": {"namespace": "astronomy-shop", "alertname": "CheckoutDown"}},
                        ]
                    }
                }
            )

    monkeypatch.setattr(prometheus_server, "ObservabilityClient", FakeClient)

    metrics = prometheus_server.get_metrics.fn("kube_pod_info")
    alerts = prometheus_server.get_alerts.fn()

    assert "checkout" in metrics
    assert "chaos-mesh" not in metrics
    assert "khaos" not in metrics
    assert "private-probe" not in metrics
    assert "sregym-verifier" not in metrics
    assert "mcp-server" not in metrics
    assert "legacy-mcp" not in metrics
    assert "node-probe" not in metrics
    assert "CheckoutDown" in alerts
    assert "chaos-mesh" not in alerts
    assert "ProbeDown" not in alerts
    assert "ControlDown" not in alerts


def test_observability_collectors_use_the_shared_hidden_namespaces():
    root = Path(__file__).resolve().parents[2]
    prometheus_values = yaml.safe_load((root / "sregym/observer/prometheus/prometheus/values.yaml").read_text())
    jobs = prometheus_values["serverFiles"]["prometheus.yml"]["scrape_configs"]
    for name in ("kube-state-metrics", "kubernetes-cadvisor"):
        job = next(job for job in jobs if job["job_name"] == name)
        namespace_rule = next(
            rule for rule in job["metric_relabel_configs"] if rule.get("source_labels") == ["namespace"]
        )
        assert namespace_rule["action"] == "drop"
        assert set(namespace_rule["regex"].split("|")) == HIDDEN_NAMESPACES

    promtail_values = yaml.safe_load((root / "sregym/observer/loki/promtail-values.yaml").read_text())
    namespace_rule = next(
        rule
        for rule in promtail_values["config"]["snippets"]["extraRelabelConfigs"]
        if rule.get("source_labels") == ["__meta_kubernetes_namespace"]
    )
    assert namespace_rule["action"] == "drop"
    assert set(namespace_rule["regex"].split("|")) == HIDDEN_NAMESPACES
