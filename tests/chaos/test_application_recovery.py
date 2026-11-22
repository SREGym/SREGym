import contextlib
import json
import subprocess
from types import SimpleNamespace

import pytest

from sregym.conductor.oracles.application_recovery import ApplicationRecoveryCheck
from sregym.conductor.oracles.apps.hotel_reservation.slo_recovery import HotelReservationRecoveryCheck
from sregym.conductor.oracles.apps.social_network.slo_recovery import SocialNetworkRecoveryCheck


class ExampleCheck(ApplicationRecoveryCheck):
    collections = {"records": ("database", "app", "records")}

    def verify_requests(self, url, records):
        if not self.works:
            raise ValueError("Incorrect response")


@pytest.fixture
def check(monkeypatch):
    check = ExampleCheck(SimpleNamespace(namespace="app", frontend_service="frontend", frontend_port=80))
    check.works = True
    check.rows = [{"id": 1, "value": "original"}]
    monkeypatch.setattr(check, "read_collection", lambda _: check.rows)
    monkeypatch.setattr(check, "frontend", lambda: contextlib.nullcontext("http://localhost"))
    monkeypatch.setattr("sregym.conductor.oracles.application_recovery.time.sleep", lambda _: None)
    check.capture_baseline()
    return check


def test_new_data_and_equivalent_replacements_are_allowed(check):
    check.rows = [{"value": "original", "id": 1}, {"id": 2, "value": "new"}]
    assert check.evaluate().success


@pytest.mark.parametrize("rows", [[], [{"id": 1, "value": "changed"}], [{"id": 2, "value": "original"}]])
def test_lost_or_changed_baseline_records_fail(check, rows):
    check.rows = rows
    result = check.evaluate()
    assert not result.success
    assert result.reason == "application_data_changed"


def test_record_counts_alone_cannot_hide_data_loss(check):
    check.rows = [{"id": 2, "value": "new"}] * 10
    assert not check.evaluate().success


def test_incorrect_response_fails_with_preserved_database(check):
    check.works = False
    assert check.evaluate().reason == "application_response_invalid"


def test_no_baseline_cannot_pass(check):
    check.baseline = None
    with pytest.raises(RuntimeError, match="pre-fault baseline"):
        check.evaluate()


def test_probe_transport_error_is_not_an_invalid_application_response(check, monkeypatch):
    def unavailable(*args):
        raise subprocess.CalledProcessError(1, ["kubectl", "exec"], stderr="workload probe connection lost")

    monkeypatch.setattr(check, "verify_requests", unavailable)
    with pytest.raises(subprocess.CalledProcessError):
        check.evaluate()


def test_isolated_application_error_can_recover_on_a_fresh_transaction(check, monkeypatch):
    attempts = []

    def verify(*args):
        attempts.append(1)
        if len(attempts) == 1:
            raise ValueError("temporary request failure")

    monkeypatch.setattr(check, "verify_requests", verify)
    assert check.evaluate().success
    assert len(attempts) == 2


def test_content_probe_uses_the_real_service_from_the_workload_job(check, monkeypatch):
    calls = []

    def kubectl(*args):
        calls.append(args)
        if args[:2] == ("get", "job"):
            return json.dumps({"metadata": {"uid": "workload-uid"}})
        if args[:2] == ("get", "pods"):
            return json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"name": name, "ownerReferences": [{"kind": "Job", "uid": owner}]},
                            "status": {"phase": "Running"},
                        }
                        for name, owner in [("unrelated", "another-uid"), ("workload-pod", "workload-uid")]
                    ]
                }
            )
        return "HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\n{}"

    monkeypatch.setattr(check, "_kubectl", kubectl)
    with ApplicationRecoveryCheck.frontend(check) as url:
        assert check.request(url, "/write", {"text": "a&b"}, post=True) == "{}"
    request = calls[-1]
    assert request[:2] == ("exec", "workload-pod")
    assert request[-3:-1] == ("frontend", "80")
    assert "POST /write HTTP/1.0" in request[-1]
    assert request[-1].endswith("text=a%26b")
    assert check._probe_pod is None


def test_unrelated_pod_cannot_become_the_workload_probe(check, monkeypatch):
    responses = iter([json.dumps({"metadata": {"uid": "workload-uid"}}), json.dumps({"items": []})])
    monkeypatch.setattr(check, "_kubectl", lambda *args: next(responses))
    with pytest.raises(RuntimeError, match="protected workload"), ApplicationRecoveryCheck.frontend(check):
        pytest.fail("A probe without a workload pod must not run")


def hotel_check(monkeypatch):
    check = HotelReservationRecoveryCheck(
        SimpleNamespace(namespace="app", frontend_service="frontend", frontend_port=5000)
    )
    monkeypatch.setattr(
        check,
        "read_collection",
        lambda name, query: [{"customerName": query["customerName"], "hotelId": "1", "number": 1}],
    )
    return check


def test_hotel_probes_require_real_content_and_persistent_reservation(monkeypatch):
    check = hotel_check(monkeypatch)
    feature = {
        "id": "1",
        "properties": {"name": "Hotel", "phone_number": "123"},
        "geometry": {"coordinates": [-122.0, 37.0]},
    }

    def request(url, path, params):
        if path == "/user":
            return json.dumps({"message": "Login successfully!"})
        if path == "/reservation":
            return json.dumps({"message": "Reserve successfully!"})
        return json.dumps({"type": "FeatureCollection", "features": [feature]})

    monkeypatch.setattr(check, "request", request)
    records = {
        "profiles": [{"id": "1", "name": "Hotel", "phoneNumber": "123", "address": {"lon": -122.0, "lat": 37.0}}]
    }
    check.verify_requests("http://localhost", records)
    monkeypatch.setattr(check, "read_collection", lambda *args: [])
    with pytest.raises(ValueError, match="persistent record"):
        check.verify_requests("http://localhost", records)


@pytest.mark.parametrize("body", [{}, {"type": "FeatureCollection", "features": []}, {"message": "OK"}])
def test_hotel_empty_success_responder_fails(monkeypatch, body):
    check = hotel_check(monkeypatch)
    monkeypatch.setattr(check, "request", lambda *args: json.dumps(body))
    with pytest.raises(ValueError, match="hotel results"):
        check.verify_requests("http://localhost", {"profiles": []})


def test_social_echo_without_database_write_fails(monkeypatch):
    check = SocialNetworkRecoveryCheck(
        SimpleNamespace(namespace="app", frontend_service="frontend", frontend_port=8080)
    )
    monkeypatch.setattr(
        check,
        "request",
        lambda url, path, *args, **kw: "Successfully upload post" if path.endswith("compose") else "Success!",
    )
    monkeypatch.setattr(check, "read_collection", lambda *args: [])
    with pytest.raises(ValueError, match="persistent record"):
        check.verify_requests("http://localhost", {})


def test_social_requires_both_timelines_to_show_the_new_post(monkeypatch):
    check = SocialNetworkRecoveryCheck(
        SimpleNamespace(namespace="app", frontend_service="frontend", frontend_port=8080)
    )
    post = {}

    def request(url, path, params, **kw):
        if path.endswith("compose"):
            post.update(text=params["text"], creator={"username": params["username"]})
            return "Successfully upload post"
        if path.endswith(("register", "follow")):
            return "Success!"
        return json.dumps([post])

    monkeypatch.setattr(check, "request", request)
    monkeypatch.setattr(check, "read_collection", lambda *args: [post])
    check.verify_requests("http://localhost", {})
    original = check.request
    monkeypatch.setattr(
        check, "request", lambda u, p, a, **kw: "[]" if "home-timeline" in p else original(u, p, a, **kw)
    )
    times = iter([0, 0, 30])
    monkeypatch.setattr("sregym.conductor.oracles.apps.social_network.slo_recovery.time.monotonic", lambda: next(times))
    monkeypatch.setattr("sregym.conductor.oracles.apps.social_network.slo_recovery.time.sleep", lambda _: None)
    with pytest.raises(ValueError, match="home-timeline"):
        check.verify_requests("http://localhost", {})
