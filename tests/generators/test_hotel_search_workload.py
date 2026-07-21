from sregym.generators.workload.hotel_search import HotelSearchMetrics, HotelSearchWorkload


def test_parse_prometheus_metrics_ignores_comments_and_invalid_samples():
    payload = """
    # HELP rate_queue_depth Requests waiting for capacity.
    rate_queue_depth 17
    search_requests_total 42
    malformed
    bad_metric not-a-number
    """

    assert HotelSearchMetrics._parse(payload) == {
        "rate_queue_depth": 17.0,
        "search_requests_total": 42.0,
    }


def test_snapshot_reports_open_loop_rate_and_completed_request_success(monkeypatch):
    workload = HotelSearchWorkload("hotel-reservation", base_rate=8)
    workload._submissions.extend([90.0, 91.0, 95.0, 99.0])
    workload._events.extend(
        [
            (91.0, False, 2.0),
            (95.0, True, 0.2),
            (99.0, True, 0.1),
        ]
    )
    monkeypatch.setattr("sregym.generators.workload.hotel_search.time.monotonic", lambda: 100.0)

    snapshot = workload.snapshot(window_seconds=5)

    assert snapshot.submitted == 2
    assert snapshot.completed == 2
    assert snapshot.succeeded == 2
    assert snapshot.actual_rate == 0.4
    assert snapshot.success_rate == 1.0
    assert snapshot.p95_latency_seconds == 0.2


def test_response_validation_requires_real_nonempty_hotel_features():
    valid = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": "hotel-1",
                "geometry": {"type": "Point", "coordinates": [-122.0, 38.0]},
            }
        ],
    }

    assert HotelSearchWorkload._valid_response(valid) is True
    assert HotelSearchWorkload._valid_response({"type": "FeatureCollection", "features": []}) is False
    assert HotelSearchWorkload._valid_response({"status": "ok"}) is False
