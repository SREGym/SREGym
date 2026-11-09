"""Check real requests and traces through Hotel Reservation's central collector.

Set HOTEL_RESERVATION_URL and JAEGER_URL to owned port-forwards before running.
"""

import os
import time
import uuid

import pytest
import requests

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def endpoints():
    frontend = os.environ.get("HOTEL_RESERVATION_URL")
    jaeger = os.environ.get("JAEGER_URL")
    if not frontend or not jaeger:
        pytest.skip("requires HOTEL_RESERVATION_URL and JAEGER_URL")
    return frontend.rstrip("/"), jaeger.rstrip("/")


def _traces(jaeger, trace_id):
    response = requests.get(f"{jaeger}/api/traces/{trace_id}", timeout=15)
    if response.status_code == 404:
        return []
    response.raise_for_status()
    return response.json().get("data") or []


@pytest.mark.parametrize(
    ("path", "params", "required"),
    [
        (
            "/hotels",
            {"inDate": "2015-04-09", "outDate": "2015-04-10", "lat": "37.7867", "lon": "-122.4112"},
            {"frontend", "search", "geo", "rate", "reservation", "profile"},
        ),
        (
            "/recommendations",
            {"require": "dis", "lat": "37.7867", "lon": "-122.4112"},
            {"frontend", "recommendation", "profile"},
        ),
        ("/user", {"username": "Cornell_0", "password": "0" * 10}, {"frontend", "user"}),
        (
            "/reservation",
            {
                "inDate": "2030-01-01",
                "outDate": "2030-01-02",
                "hotelId": "1",
                "username": "Cornell_0",
                "password": "0" * 10,
                "number": "1",
            },
            {"frontend", "user", "reservation"},
        ),
    ],
    ids=["hotels", "recommendations", "login", "reservation"],
)
def test_request_exports_connected_spans(endpoints, path, params, required, record_property):
    frontend, jaeger = endpoints
    trace_id, parent = uuid.uuid4().hex, "1234567890abcdef"
    record_property("trace_id", trace_id)
    if path == "/reservation":
        params = {**params, "customerName": f"trace-{trace_id}"}
    response = requests.get(
        frontend + path,
        params=params,
        headers={"uber-trace-id": f"{trace_id}:{parent}:0:1"},
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()
    if path in {"/hotels", "/recommendations"}:
        assert body["features"], body
    else:
        assert body["message"] == ("Login successfully!" if path == "/user" else "Reserve successfully!"), body

    deadline = time.monotonic() + 45
    traces, services, missing_parents = [], set(), set()
    while time.monotonic() < deadline:
        traces = _traces(jaeger, trace_id)
        if traces:
            trace = traces[0]
            spans = trace["spans"]
            services = {trace["processes"][span["processID"]]["serviceName"] for span in spans}
            span_ids = {span["spanID"] for span in spans}
            missing_parents = {
                ref["spanID"]
                for span in spans
                for ref in span.get("references", [])
                if ref["refType"] == "CHILD_OF" and ref["spanID"] not in span_ids | {parent}
            }
            if required <= services and not missing_parents:
                return
        time.sleep(2)
    assert traces, "The request succeeded but its trace did not reach central Jaeger"
    assert required <= services, f"Missing services: {required - services}"
    assert not missing_parents, f"Unexported parents: {missing_parents}"


def test_unsampled_request_stays_unsampled(endpoints, record_property):
    frontend, jaeger = endpoints
    trace_id = uuid.uuid4().hex
    record_property("trace_id", trace_id)
    response = requests.get(
        frontend + "/hotels",
        params={"inDate": "2015-04-09", "outDate": "2015-04-10", "lat": "37.7867", "lon": "-122.4112"},
        headers={"uber-trace-id": f"{trace_id}:1234567890abcdef:0:0"},
        timeout=30,
    )
    response.raise_for_status()
    assert response.json()["features"]
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        assert not _traces(jaeger, trace_id), "The unsampled request produced spans"
        time.sleep(2)
