from mcp_server import prometheus_server


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
    assert "CheckoutDown" in alerts
    assert "chaos-mesh" not in alerts
