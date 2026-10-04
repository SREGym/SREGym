"""Exercise the deployed fan-out function and real workload through the oracle."""

import ast
import random
import threading
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from sregym.conductor.oracles.thundering_herd_mitigation import ThunderingHerdMitigationOracle
from sregym.generators.workload.recommendation_herd import RecommendationHerdWorkload

_ASSET = (
    Path(__file__).resolve().parents[2] / "sregym/conductor/problems/assets/thundering_herd_cascade_recommendation.py"
)


@pytest.mark.parametrize("repair", ["noop", "one_call", "warm_cache"])
def test_deployed_fault_rejects_noop_and_accepts_real_repairs(monkeypatch, repair):
    source = _ASSET.read_text(encoding="utf-8-sig")
    if repair == "one_call":
        source = source.replace("for _ in range(10):", "for _ in range(1):")
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "get_product_list")
    product_ids = {"OLJCESPC7Z", "66VCHSJNUP", *(f"product-{index}" for index in range(7))}
    catalog = SimpleNamespace(products=[SimpleNamespace(id=product_id) for product_id in sorted(product_ids)])
    counts = {"products": 0, "recommendations": 0}
    lock = threading.Lock()
    logs = []

    def list_products(_):
        with lock:
            counts["products"] += 1
        return catalog

    def service_log(message, **kwargs):
        with lock:
            logs.append((datetime.now(UTC), message))

    tracer = Mock()
    tracer.start_as_current_span.side_effect = lambda _: nullcontext(Mock())
    namespace = {
        "tracer": tracer,
        "random": random.Random(0),
        "product_catalog_stub": SimpleNamespace(ListProducts=list_products),
        "demo_pb2": SimpleNamespace(Empty=lambda: None),
        "print": service_log,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(_ASSET), "exec"), namespace)
    get_product_list = namespace["get_product_list"]
    # Warm the actual catalog data before measuring cached recommendations.
    cached_catalog = list_products(None)
    if repair == "warm_cache":
        namespace["product_catalog_stub"] = SimpleNamespace(ListProducts=lambda _: cached_catalog)

    def get(url, **kwargs):
        parsed = urlsplit(url)
        response = Mock(status_code=200)
        if parsed.path == "/api/products":
            response.json.return_value = {"products": [{"id": product.id} for product in list_products(None).products]}
            return response
        assert parsed.path == "/api/recommendations"
        values = parse_qs(parsed.query).get("productIds", [])
        # Match the real Next.js query and protobuf serializer, including its
        # single-string character behavior rather than silently fixing input.
        exclusions = tuple(values[0]) if len(values) == 1 else tuple(values)
        with lock:
            counts["recommendations"] += 1
        ids = get_product_list(exclusions)
        response.json.return_value = {"productIds": ids}
        return response

    def kubectl_logs(command, **kwargs):
        since_time = next(arg.split("=", 1)[1] for arg in command.split() if arg.startswith("--since-time="))
        since = datetime.fromisoformat(since_time)
        with lock:
            return "\n".join(message for timestamp, message in logs if timestamp >= since)

    def counter(name):
        with lock:
            return float(counts[name])

    workload = RecommendationHerdWorkload("astronomy-shop", requests_per_second=100.0)
    workload.frontend = Mock()
    workload.frontend.start.return_value = 8080
    problem = SimpleNamespace(
        namespace="astronomy-shop",
        recommendation_deployment="recommendation",
        workload=workload,
        kubectl=SimpleNamespace(exec_command_checked=kubectl_logs),
    )
    oracle = ThunderingHerdMitigationOracle(problem)
    oracle.wave_seconds = 0.2
    oracle.scrape_wait_seconds = 0.001
    oracle.poll_interval_seconds = 0.001
    oracle.wave_gap_seconds = 0
    oracle._cluster_shape_unhealthy = Mock(return_value=None)
    oracle._capacity_changed = Mock(return_value=None)
    oracle._catalog_list_products_total = lambda: counter("products")
    oracle._list_recommendations_total = lambda: counter("recommendations")
    monkeypatch.setattr("sregym.generators.workload.recommendation_herd.requests.get", get)

    if repair == "noop":
        oracle.assert_fault_present()
    result = oracle.evaluate()

    if repair == "noop":
        assert result["success"] is False
        assert result["reason"] == "fault_still_present"
        assert result["detail"]["amplification"] == 10.0
    else:
        assert result == {"success": True}
    assert counts["recommendations"] >= 5
    assert not workload.background_running
