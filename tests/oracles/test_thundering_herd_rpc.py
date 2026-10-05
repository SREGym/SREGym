from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.oracles import thundering_herd_rpc as rpc


def _transport(monkeypatch, wire_response, *, error=None):
    tunnel = Mock()
    tunnel.start.return_value = 12345
    forward = Mock(return_value=tunnel)
    monkeypatch.setattr(rpc, "KubectlPortForward", forward)
    requests = []

    class Channel:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def unary_unary(self, method, response_deserializer):
            def call(request, timeout):
                requests.append((method, request, timeout))
                if error:
                    raise error
                return response_deserializer(wire_response)

            return call

    monkeypatch.setattr(rpc.grpc, "insecure_channel", lambda _: Channel())
    service = SimpleNamespace(spec=SimpleNamespace(ports=[SimpleNamespace(name="grpc", port=8081)]))
    reader = Mock(return_value=service)
    problem = SimpleNamespace(
        namespace="astronomy-shop",
        kubectl=SimpleNamespace(core_v1_api=SimpleNamespace(read_namespaced_service=reader)),
        workload=SimpleNamespace(catalog_product_ids=Mock(side_effect=AssertionError("Do not probe a frontend cache"))),
    )
    return problem, requests, forward, tunnel, reader


def test_catalog_probe_uses_public_rpc_and_ignores_unneeded_product_metadata(monkeypatch):
    # Stock proto: products field1; Product.id field1 and Product.name field2.
    wire_response = b"\x0a\x10\x0a\x08valid-id\x12\x04star"
    problem, requests, forward, tunnel, reader = _transport(monkeypatch, wire_response)

    assert rpc.catalog_product_ids(problem) == {"valid-id"}

    assert requests == [("/oteldemo.ProductCatalogService/ListProducts", b"", 15)]
    forward.assert_called_once_with("astronomy-shop", "product-catalog", 8081)
    reader.assert_called_once_with(name="product-catalog", namespace="astronomy-shop", _request_timeout=10)
    tunnel.stop.assert_called_once()
    problem.workload.catalog_product_ids.assert_not_called()


def test_recommendation_probe_sends_complete_exclusion_ids(monkeypatch):
    problem, requests, forward, tunnel, _ = _transport(monkeypatch, b"\x0a\x08valid-id")

    assert rpc.recommendation_product_ids(problem, ("excluded",)) == ("valid-id",)

    assert requests == [("/oteldemo.RecommendationService/ListRecommendations", b"\x12\x08excluded", 15)]
    forward.assert_called_once_with("astronomy-shop", "recommendation", 8081)
    tunnel.stop.assert_called_once()


def test_rpc_probe_closes_the_tunnel_when_the_call_fails(monkeypatch):
    problem, _, _, tunnel, _ = _transport(monkeypatch, b"", error=RuntimeError("transport failure"))

    with pytest.raises(RuntimeError, match="transport failure"):
        rpc.catalog_product_ids(problem)

    tunnel.stop.assert_called_once()
