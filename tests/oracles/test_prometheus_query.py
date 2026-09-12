import json
from unittest.mock import Mock

from sregym.conductor.oracles.prometheus_query import (
    catalog_list_products_total,
    prometheus_query_url,
    prometheus_scalar,
)


def test_prometheus_query_url_encodes_selectors():
    url = prometheus_query_url('sum(traces_span_metrics_calls_total{span_name=~".*ListProducts.*"})')
    assert url.startswith("http://localhost:9090/api/v1/query?query=")
    assert " " not in url.split("query=", 1)[1]
    assert "ListProducts" in url


def test_prometheus_scalar_sums_vector_samples(monkeypatch):
    payload = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {}, "value": [1, "10.5"]},
                {"metric": {}, "value": [1, "2.5"]},
            ],
        },
    }
    monkeypatch.setattr(
        "sregym.conductor.oracles.prometheus_query.subprocess.check_output",
        Mock(return_value=json.dumps(payload)),
    )
    assert prometheus_scalar("up") == 13.0


def test_prometheus_scalar_empty_vector_is_zero(monkeypatch):
    payload = {"status": "success", "data": {"resultType": "vector", "result": []}}
    monkeypatch.setattr(
        "sregym.conductor.oracles.prometheus_query.subprocess.check_output",
        Mock(return_value=json.dumps(payload)),
    )
    assert prometheus_scalar("up") == 0.0


def test_catalog_list_products_prefers_namespaced_query(monkeypatch):
    calls = []

    def fake_scalar(query, *, announce=True):
        calls.append(query)
        if "namespace=" in query:
            return 42.0
        return 99.0

    monkeypatch.setattr("sregym.conductor.oracles.prometheus_query.prometheus_scalar", fake_scalar)
    assert catalog_list_products_total("astronomy-shop") == 42.0
    assert "namespace=\"astronomy-shop\"" in calls[0]
    assert "ListProducts" in calls[0]
    assert "product-catalog" in calls[0]
    assert "recommendation" in calls[0]


def test_catalog_list_products_falls_back_without_namespace(monkeypatch):
    def fake_scalar(query, *, announce=True):
        if "namespace=" in query:
            return None
        return 7.0

    monkeypatch.setattr("sregym.conductor.oracles.prometheus_query.prometheus_scalar", fake_scalar)
    assert catalog_list_products_total("astronomy-shop") == 7.0


def test_catalog_list_products_keeps_namespaced_zero(monkeypatch):
    def fake_scalar(query, *, announce=True):
        if "namespace=" in query:
            return 0.0
        raise AssertionError("must not fall back to leftover unscoped series")

    monkeypatch.setattr("sregym.conductor.oracles.prometheus_query.prometheus_scalar", fake_scalar)
    assert catalog_list_products_total("astronomy-shop") == 0.0


def test_catalog_list_products_fails_closed_when_prom_is_down(monkeypatch):
    monkeypatch.setattr(
        "sregym.conductor.oracles.prometheus_query.prometheus_scalar",
        lambda *args, **kwargs: None,
    )
    assert catalog_list_products_total("astronomy-shop") is None
