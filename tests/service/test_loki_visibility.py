from mcp_server import loki_server


class FakeResponse:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


def test_loki_tools_hide_chaos_streams_and_label_values(monkeypatch):
    class FakeClient:
        def __init__(self, _url):
            pass

        def make_request(self, _method, url, **_kwargs):
            if url.endswith("query_range"):
                return FakeResponse(
                    {
                        "status": "success",
                        "data": {
                            "result": [
                                {
                                    "stream": {"namespace": "chaos-mesh", "pod": "chaos-controller-manager"},
                                    "values": [["1000000000", "controller applied PodChaos"]],
                                },
                                {
                                    "stream": {"namespace": "astronomy-shop", "pod": "checkout"},
                                    "values": [
                                        ["1000000000", "checkout completed"],
                                        ["2000000000", "chaos-mesh injected a disruption"],
                                    ],
                                },
                            ]
                        },
                    }
                )
            return FakeResponse({"status": "success", "data": ["chaos-mesh", "astronomy-shop"]})

    monkeypatch.setattr(loki_server, "ObservabilityClient", FakeClient)

    logs = loki_server.get_logs.fn('{namespace=~".+"}')
    values = loki_server.get_label_values.fn("namespace")

    assert "checkout completed" in logs
    assert "chaos-mesh" not in logs
    assert "chaos-mesh" not in values
    assert "astronomy-shop" in values
