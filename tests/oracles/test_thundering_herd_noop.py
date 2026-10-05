"""Exercise the deployed fan-out function and real workload through the oracle."""

import ast
import random
import threading
import time
from contextlib import nullcontext
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


@pytest.mark.parametrize(
    "repair", ["noop", "noop_stalled_catalog", "noop_stalled_during_wave", "one_call", "warm_cache", "size_cache"]
)
def test_deployed_fault_rejects_noop_and_accepts_real_repairs(monkeypatch, repair):
    source = _ASSET.read_text(encoding="utf-8-sig")
    if repair in {"one_call", "size_cache"}:
        source = source.replace(
            "candidates = [_catalog_product(product_id) for product_id in candidate_ids]",
            "candidates = [product for product in cat_response.products if product.id in candidate_ids]",
        )
    tree = ast.parse(source)
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in {"_catalog_product", "get_product_list"}
    ]
    product_ids = {"OLJCESPC7Z", "66VCHSJNUP", *(f"product-{index}" for index in range(8))}
    catalog = SimpleNamespace(products=[SimpleNamespace(id=product_id) for product_id in sorted(product_ids)])
    counts = {"products": 0, "recommendations": 0}
    lock = threading.Lock()

    def list_products(_):
        with lock:
            counts["products"] += 1
        return catalog

    tracer = Mock()
    tracer.start_as_current_span.side_effect = lambda _: nullcontext(Mock())
    namespace = {
        "tracer": tracer,
        "random": random.Random(0),
        "product_catalog_stub": SimpleNamespace(ListProducts=list_products),
        "demo_pb2": SimpleNamespace(Empty=lambda: None),
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(_ASSET), "exec"), namespace)
    get_product_list = namespace["get_product_list"]
    recommendation_cache = {}
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
        if repair == "size_cache":
            with lock:
                ids = recommendation_cache.get(len(exclusions))
            if ids is None:
                ids = get_product_list(exclusions)
                with lock:
                    recommendation_cache[len(exclusions)] = ids
        else:
            ids = get_product_list(exclusions)
        # The real frontend fetches product objects for the first four RPC
        # results, rather than returning all five service recommendations.
        response.json.return_value = [{"id": product_id} for product_id in ids[:4]]
        return response

    def counter(name):
        with lock:
            return float(counts[name])

    workload = RecommendationHerdWorkload("astronomy-shop", requests_per_second=100.0)
    workload.frontend = Mock()
    workload.frontend.start.return_value = 8080
    one_request = workload._one_request

    def request_with_simulated_latency(exclusions):
        ok, _, ids = one_request(exclusions)
        # The in-process HTTP responder has no network delay. Host scheduling
        # pauses must not turn this RPC/output regression into a service SLO
        # failure; latency rejection is checked separately and on live KIND.
        return ok, 0.001, ids

    workload._one_request = request_with_simulated_latency
    simulated_clock = {"elapsed": 0.0}
    clock_lock = threading.Lock()

    def advance_clock(delay):
        with clock_lock:
            simulated_clock["elapsed"] += delay
        time.sleep(0)

    monkeypatch.setattr(
        "sregym.generators.workload.recommendation_herd.time",
        SimpleNamespace(
            monotonic=lambda: simulated_clock["elapsed"],
            sleep=advance_clock,
            time_ns=time.time_ns,
        ),
    )
    problem = SimpleNamespace(
        namespace="astronomy-shop",
        recommendation_deployment="recommendation",
        workload=workload,
        kubectl=SimpleNamespace(
            exec_command_checked=Mock(side_effect=AssertionError("RPC grading does not read logs"))
        ),
    )
    oracle = ThunderingHerdMitigationOracle(problem)
    oracle._baseline_catalog_ids = set(product_ids)
    oracle._catalog_product_ids = workload.catalog_product_ids

    def recommendation_probe(exclusions):
        with lock:
            counts["recommendations"] += 1
        return tuple(get_product_list(exclusions))

    oracle._recommendation_product_ids = recommendation_probe
    # Paced virtual time keeps the sample count independent of host pauses.
    oracle.wave_seconds = 1.5
    oracle.scrape_wait_seconds = 0.001
    oracle.poll_interval_seconds = 0.001
    oracle.wave_gap_seconds = 0
    oracle._cluster_shape_unhealthy = Mock(return_value=None)
    oracle._capacity_changed = Mock(return_value=None)
    oracle._catalog_list_products_total = lambda: counter("products")
    oracle._list_recommendations_total = lambda: counter("recommendations")
    monkeypatch.setattr("sregym.generators.workload.recommendation_herd.requests.get", get)

    class InProcessSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, **kwargs):
            return get(url, **kwargs)

    monkeypatch.setattr(
        "sregym.generators.workload.recommendation_herd.requests.Session",
        InProcessSession,
    )

    if repair in {"noop", "noop_stalled_catalog", "noop_stalled_during_wave"}:
        oracle.assert_fault_present()
    reported_catalog = {"frozen": None}
    if repair == "noop_stalled_catalog":
        # The injected service still makes ten actual catalog calls, but its
        # exporter stopped after injection. This must not pass as a warm cache.
        reported_catalog["frozen"] = counter("products")
    if repair == "noop_stalled_during_wave":
        original_run = workload.run

        def run_without_catalog_export(**kwargs):
            if reported_catalog["frozen"] is None:
                reported_catalog["frozen"] = counter("products")
            return original_run(**kwargs)

        workload.run = run_without_catalog_export
    oracle._catalog_list_products_total = lambda: (
        counter("products") if reported_catalog["frozen"] is None else reported_catalog["frozen"]
    )
    result = oracle.evaluate()

    if repair in {"noop_stalled_catalog", "noop_stalled_during_wave"}:
        assert result["success"] is False
        assert result["reason"] == "rpc_telemetry_stalled"
        assert counter("products") > reported_catalog["frozen"]
    elif repair == "noop":
        assert result["success"] is False
        assert result["reason"] == "fault_still_present"
        assert result["detail"]["amplification"] == 10.0
    elif repair == "size_cache":
        assert result["success"] is False
        assert result["reason"] == "invalid_recommendation_ids"
    else:
        assert result == {"success": True}
    assert counts["recommendations"] >= 5
    assert not workload.background_running
