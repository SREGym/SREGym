import threading
from unittest.mock import Mock

import pytest

from sregym.generators.workload.recommendation_herd import RecommendationHerdWorkload, extract_product_ids


def test_extract_product_ids_from_product_ids_key():
    assert extract_product_ids({"productIds": ["OLJCESPC7Z", "66VCHSJNUP"]}) == [
        "OLJCESPC7Z",
        "66VCHSJNUP",
    ]


def test_extract_product_ids_from_nested_products():
    payload = {"products": [{"id": "A"}, {"id": "B"}, {"name": "no-id"}]}
    assert extract_product_ids(payload) == ["A", "B"]


def test_extract_product_ids_from_list_payload():
    assert extract_product_ids(["x", {"id": "y"}]) == ["x", "y"]


def test_extract_product_ids_empty_on_unknown_shape():
    assert extract_product_ids({"ok": True}) == []
    assert extract_product_ids(None) == []


def _workload(**kwargs):
    workload = RecommendationHerdWorkload("astronomy-shop", **kwargs)
    workload.frontend = Mock()
    return workload


def test_fast_responder_is_paced_instead_of_saturating_the_catalog():
    workload = _workload()
    workload._one_request = Mock(return_value=(True, 0.001, ("a", "b")))

    snapshot = workload.run(concurrency=2, duration_seconds=0.15, product_ids=("excluded",))

    # The default two requests/second allows one initial two-worker burst,
    # then a one-second pause, even when recommendations complete instantly.
    assert 1 <= snapshot.submitted <= 2
    assert snapshot.succeeded == snapshot.submitted


def test_shuffling_fixed_ids_does_not_create_distinct_recommendation_sets():
    workload = _workload(requests_per_second=20.0)
    counter = 0

    def responder(_):
        nonlocal counter
        counter += 1
        return True, 0.001, ("a", "b") if counter % 2 else ("b", "a")

    workload._one_request = responder

    snapshot = workload.run(concurrency=1, duration_seconds=0.12, product_ids=("excluded",))

    assert snapshot.succeeded >= 2
    assert snapshot.distinct_recommendation_sets == 1
    assert set(snapshot.product_ids) == {"a", "b"}


def test_background_traffic_continues_and_can_be_paused_and_resumed():
    workload = _workload(requests_per_second=20.0)
    observed = threading.Event()
    lock = threading.Lock()
    count = 0

    def responder(_):
        nonlocal count
        with lock:
            count += 1
            if count >= 6:
                observed.set()
        return True, 0.001, ("a",)

    workload._one_request = responder
    try:
        workload.start_background(concurrency=2, product_ids=("excluded",))
        original_threads = tuple(workload._background_threads)
        workload.start_background(concurrency=2, product_ids=("excluded",))
        assert tuple(workload._background_threads) == original_threads
        assert observed.wait(timeout=2.0)
        assert workload.background_running

        workload.stop_background()
        assert not workload.background_running
        assert all(not thread.is_alive() for thread in original_threads)
        workload.frontend.stop.assert_not_called()

        observed.clear()
        workload.start_background(concurrency=2, product_ids=("excluded",))
        assert observed.wait(timeout=2.0)
        assert workload.background_running
    finally:
        workload.stop()
    assert not workload.background_running
    workload.frontend.stop.assert_called_once()


def test_stop_drains_background_requests_before_closing_the_tunnel():
    workload = _workload()
    entered = threading.Event()
    release = threading.Event()

    def responder(_):
        entered.set()
        assert release.wait(timeout=2.0)
        return True, 0.001, ("a",)

    workload._one_request = responder
    workload.start_background(concurrency=1, product_ids=("excluded",))
    stop_thread = threading.Thread(target=workload.stop)
    try:
        assert entered.wait(timeout=2.0)
        stop_thread.start()
        workload.frontend.stop.assert_not_called()
    finally:
        release.set()
        stop_thread.join(timeout=2.0)
        workload.stop_background()
    assert not stop_thread.is_alive()
    workload.frontend.stop.assert_called_once()


def test_background_traffic_survives_a_port_forward_rollout_error(monkeypatch):
    workload = _workload(requests_per_second=20.0)
    recovered = threading.Event()
    workload.frontend.start.side_effect = [RuntimeError("frontend tunnel interrupted"), 8080]

    def responder(*args, **kwargs):
        recovered.set()
        response = Mock(status_code=200)
        response.json.return_value = {"productIds": ["a"]}
        return response

    monkeypatch.setattr("sregym.generators.workload.recommendation_herd.requests.get", responder)
    try:
        workload.start_background(concurrency=1, product_ids=("excluded",))
        assert recovered.wait(timeout=2.0)
        assert workload.background_running
    finally:
        workload.stop()


@pytest.mark.parametrize("rate", [0, -1, float("nan"), float("inf")])
def test_nonpositive_request_rate_is_rejected(rate):
    with pytest.raises(ValueError, match="must be positive"):
        RecommendationHerdWorkload("astronomy-shop", requests_per_second=rate)
