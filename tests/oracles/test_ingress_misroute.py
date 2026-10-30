import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from kubernetes.client import Configuration
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import ReadTimeoutError

from sregym.conductor.oracles import ingress_misroute_oracle as module
from sregym.conductor.oracles.ingress_misroute_oracle import IngressMisrouteMitigationOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.apps.slack_spine.networking import IngressMisrouteSlack
from sregym.conductor.problems.ingress_misroute import IngressMisroute

HOTEL = {
    "type": "FeatureCollection",
    "features": [{"id": "40", "properties": {"name": "St. Regis San Francisco"}}],
}


@pytest.fixture
def oracle(monkeypatch):
    problem = SimpleNamespace(
        kubectl=Mock(),
        namespace="hotel-reservation",
        path="/api",
        ingress_probe_path="/api/recommendations?require=dis&lat=38.0235&lon=-122.095",
        ingress_response_valid=IngressMisroute.ingress_response_valid,
    )
    oracle = IngressMisrouteMitigationOracle(problem)
    monkeypatch.setattr(MitigationOracle, "evaluate", Mock(return_value={"success": True}))
    monkeypatch.setattr(oracle, "_request_route", Mock(return_value=(200, HOTEL)))
    monkeypatch.setattr(oracle, "ROUTE_SETTLE_SECONDS", 0)
    monkeypatch.setattr(oracle, "REQUIRED_SUCCESSES", 1)
    return oracle


def test_real_hotel_response_passes_without_fixed_backend_name_or_port(oracle):
    assert oracle.evaluate() == {"success": True}
    oracle._request_route.assert_called_once()


@pytest.mark.parametrize("body", [b"OK", b"<html>healthy</html>", None, [], {}, {"ok": True}])
def test_unrelated_http_200_is_not_recovery(oracle, body):
    oracle._request_route.return_value = 200, body
    assert oracle.evaluate()["reason"] == "connectivity_probe_failed"


@pytest.mark.parametrize(
    "payload",
    [
        {"features": HOTEL["features"]},
        {"type": "FeatureCollection", "features": []},
        {"type": "FeatureCollection", "features": [None]},
        {"type": "FeatureCollection", "features": [{"id": "40"}]},
        {"type": "FeatureCollection", "features": [{"id": "", "properties": {"name": "Hotel"}}]},
        {"type": "FeatureCollection", "features": [{"id": "40", "properties": {"name": ""}}]},
    ],
)
def test_missing_hotel_data_fails(oracle, payload):
    oracle._request_route.return_value = 200, payload
    assert oracle.evaluate()["success"] is False


@pytest.mark.parametrize("status", [301, 404, 500, 502, 503, 504])
def test_non_200_fails_even_with_expected_body(oracle, status):
    oracle._request_route.return_value = status, HOTEL
    assert oracle.evaluate()["success"] is False


@pytest.mark.parametrize("status", [404, 500, 502, 503, 504])
def test_route_errors_do_not_claim_kubernetes_api_failure(oracle, status):
    oracle._request_route.side_effect = ApiException(status=status)
    result = oracle.evaluate()
    assert result["reason"] == "connectivity_probe_failed"
    assert result["failure_class"] == "ambiguous"


@pytest.mark.parametrize("status", [401, 403, 429])
def test_other_api_errors_keep_normal_classification(oracle, status):
    error = ApiException(status=status)
    oracle._request_route.side_effect = error
    assert oracle.evaluate() == oracle.fail_from_exception(error)


@pytest.mark.parametrize(
    "error",
    [
        OSError("connection failed"),
        ReadTimeoutError(None, None, "timeout"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid response"),
    ],
)
def test_transport_errors_cannot_pass(oracle, error):
    oracle._request_route.side_effect = error
    assert oracle.evaluate()["reason"] == "connectivity_probe_failed"


def test_route_update_can_settle_before_rejection(oracle, monkeypatch):
    oracle.ROUTE_SETTLE_SECONDS = 30
    oracle._request_route.side_effect = [ApiException(status=503), (200, HOTEL)]
    sleep = Mock()
    monkeypatch.setattr(module.time, "sleep", sleep)
    assert oracle.evaluate()["success"] is True
    assert oracle._request_route.call_count == 2
    sleep.assert_called_once_with(oracle.POLL_SECONDS)


def test_recovery_requires_consecutive_responses(oracle, monkeypatch):
    oracle.REQUIRED_SUCCESSES = 3
    oracle.ROUTE_SETTLE_SECONDS = 30
    oracle._request_route.side_effect = [(200, HOTEL), ApiException(status=503), *(3 * [(200, HOTEL)])]
    monkeypatch.setattr(module.time, "sleep", Mock())
    assert oracle.evaluate()["success"] is True
    assert oracle._request_route.call_count == 5


def test_old_working_configuration_cannot_hide_a_bad_update(oracle, monkeypatch):
    oracle.REQUIRED_SUCCESSES = 3
    oracle.ROUTE_SETTLE_SECONDS = 4
    oracle._request_route.side_effect = [(200, HOTEL), ApiException(status=503), ApiException(status=503)]
    monkeypatch.setattr(module.time, "monotonic", Mock(side_effect=[0, 0, 2, 4]))
    monkeypatch.setattr(module.time, "sleep", Mock())
    assert oracle.evaluate()["success"] is False


def test_shared_workload_failure_short_circuits_probe(oracle, monkeypatch):
    failure = oracle.fail("required_deployment_scaled_to_zero", deployment="frontend")
    monkeypatch.setattr(MitigationOracle, "evaluate", lambda self: failure)
    assert oracle.evaluate() == failure
    oracle._request_route.assert_not_called()


def test_request_traverses_ingress_controller_and_preserves_query(oracle, monkeypatch):
    api = MagicMock()
    api.call_api.return_value = HOTEL, 200, {}
    context = MagicMock()
    context.__enter__.return_value = api
    monkeypatch.setattr(module.client, "ApiClient", lambda *_: context)
    status, body = IngressMisrouteMitigationOracle._request_route(oracle)
    assert status == 200 and body == HOTEL
    args, kwargs = api.call_api.call_args
    assert args == (
        "/api/v1/namespaces/ingress-nginx/services/http:ingress-nginx-controller:80/proxy/api/recommendations",
        "GET",
    )
    assert kwargs["query_params"] == [("require", "dis"), ("lat", "38.0235"), ("lon", "-122.095")]
    assert kwargs["_request_timeout"] == (5, 10)
    assert kwargs["auth_settings"] == ["BearerToken"]
    context.__exit__.assert_called_once()


def test_probe_path_uses_the_configured_prefix():
    problem = SimpleNamespace(path="/hotel")
    assert IngressMisroute.ingress_probe_path.fget(problem).startswith("/hotel/recommendations?")
    assert IngressMisrouteSlack.ingress_probe_path.fget(problem) == "/hotel/channels/chan-0/messages?limit=1"


@pytest.mark.parametrize("payload,valid", [({"messages": []}, True), ({"ok": True}, False), (HOTEL, False)])
def test_slack_consumer_uses_message_history_not_hotel_data(oracle, payload, valid):
    oracle.problem.ingress_response_valid = IngressMisrouteSlack.ingress_response_valid
    oracle._request_route.return_value = 200, payload
    assert oracle.evaluate()["success"] is valid


@pytest.mark.parametrize(
    "status,payload", [(200, HOTEL), (200, "unrelated page"), (302, HOTEL), (503, {"error": "no backend"})]
)
def test_real_api_client_preserves_json_and_http_errors(
    oracle, monkeypatch, status, payload, allow_kubernetes_test_server
):
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            self.send_response(200 if self.path == "/unexpected" else status)
            if status == 302 and self.path != "/unexpected":
                self.send_header("Location", "/unexpected")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    allow_kubernetes_test_server(server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    configuration = Configuration(host=f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(module.client.Configuration, "get_default_copy", lambda: configuration)
    try:
        if status == 200:
            assert IngressMisrouteMitigationOracle._request_route(oracle) == (status, payload)
        else:
            with pytest.raises(ApiException) as exc:
                IngressMisrouteMitigationOracle._request_route(oracle)
            assert exc.value.status == status
        assert paths[0].endswith("/proxy/api/recommendations?require=dis&lat=38.0235&lon=-122.095")
        assert len(paths) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
