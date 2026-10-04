from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from sregym.conductor.oracles.thundering_herd_mitigation import ThunderingHerdMitigationOracle
from sregym.generators.workload.recommendation_herd import HerdSnapshot


def test_otlp_span_flush_fits_the_isolated_wave_export_window():
    manifest = Path(__file__).resolve().parents[2] / "sregym/observer/otel_collector/otel-collector.yaml"
    config_map = next(yaml.safe_load_all(manifest.read_text()))
    collector = yaml.safe_load(config_map["data"]["config.yaml"])
    connector = collector["connectors"]["spanmetrics/otlp"]
    # Reserve the rest of the 45s window for SDK/collector batches and the
    # central Prometheus's 15s scrape, rather than a 60s connector default.
    assert float(connector["metrics_flush_interval"].removesuffix("s")) <= 5


def _deployment(*, replicas=1, generation=2, observed=2, ready=1, cpu_limit="200m"):
    container = SimpleNamespace(
        name="recommendation",
        resources=SimpleNamespace(
            requests={"cpu": "100m", "memory": "128Mi"},
            limits={"cpu": cpu_limit, "memory": "256Mi"},
        ),
    )
    return SimpleNamespace(
        metadata=SimpleNamespace(generation=generation),
        spec=SimpleNamespace(
            replicas=replicas,
            template=SimpleNamespace(spec=SimpleNamespace(containers=[container])),
        ),
        status=SimpleNamespace(
            observed_generation=observed,
            replicas=replicas,
            updated_replicas=replicas,
            ready_replicas=ready,
            available_replicas=ready,
            unavailable_replicas=0,
        ),
    )


def _snapshot(**overrides) -> HerdSnapshot:
    payload = dict(
        submitted=40,
        completed=40,
        succeeded=40,
        success_rate=1.0,
        p95_latency_seconds=0.4,
        p99_latency_seconds=0.6,
        product_ids=("OLJCESPC7Z", "66VCHSJNUP", "1YMWWN1N4O"),
        distinct_recommendation_sets=3,
    )
    payload.update(overrides)
    return HerdSnapshot(**payload)


def _oracle():
    catalog = {"OLJCESPC7Z", "66VCHSJNUP", "1YMWWN1N4O", "HQTGWGPNH4"}
    workload = SimpleNamespace(
        start=Mock(),
        stop=Mock(),
        background_running=False,
        stop_background=Mock(),
        catalog_product_ids=Mock(return_value=catalog),
        run=Mock(
            side_effect=lambda **kwargs: _snapshot(product_ids=tuple(sorted(catalog.difference(kwargs["product_ids"]))))
        ),
    )
    kubectl = SimpleNamespace(
        apps_v1_api=SimpleNamespace(list_namespaced_deployment=Mock()),
        core_v1_api=SimpleNamespace(read_namespaced_endpoints=Mock()),
        exec_command_checked=Mock(return_value=""),
    )
    problem = SimpleNamespace(
        namespace="astronomy-shop",
        kubectl=kubectl,
        workload=workload,
        recommendation_deployment="recommendation",
        start_workload=Mock(),
    )
    oracle = ThunderingHerdMitigationOracle(problem)
    oracle.scrape_wait_seconds = 5.0
    return oracle


def test_rollout_requires_current_ready_nonzero_replicas():
    assert ThunderingHerdMitigationOracle._rollout_complete(_deployment()) is True
    assert ThunderingHerdMitigationOracle._rollout_complete(_deployment(replicas=0, ready=0)) is False
    assert ThunderingHerdMitigationOracle._rollout_complete(_deployment(observed=1)) is False
    assert ThunderingHerdMitigationOracle._rollout_complete(_deployment(ready=0)) is False


def test_incomplete_guarded_baseline_cannot_pass():
    oracle = _oracle()
    oracle._baseline_deployments = {"recommendation", "product-catalog"}

    failure = oracle._cluster_shape_unhealthy()

    assert failure["reason"] == "baseline_not_captured"


def test_wave_healthy_accepts_low_amplification_and_real_ids():
    oracle = _oracle()
    catalog = {"OLJCESPC7Z", "66VCHSJNUP", "1YMWWN1N4O", "HQTGWGPNH4"}
    assert oracle._wave_healthy(_snapshot(), 1.2, catalog, concurrency=8) is True


def test_wave_healthy_rejects_high_amplification():
    oracle = _oracle()
    catalog = {"OLJCESPC7Z", "66VCHSJNUP", "1YMWWN1N4O"}
    assert oracle._wave_healthy(_snapshot(), 9.5, catalog, concurrency=8) is False


def test_wave_healthy_accepts_warm_catalog_cache():
    oracle = _oracle()
    catalog = {"OLJCESPC7Z", "66VCHSJNUP", "1YMWWN1N4O"}
    assert oracle._wave_healthy(_snapshot(), 0.0, catalog, concurrency=8) is True


def test_wave_healthy_rejects_empty_and_unknown_ids():
    oracle = _oracle()
    catalog = {"OLJCESPC7Z"}
    assert oracle._wave_healthy(_snapshot(product_ids=()), 1.0, catalog, concurrency=8) is False
    assert oracle._wave_healthy(_snapshot(product_ids=("not-a-product",)), 1.0, catalog, concurrency=8) is False


def test_wave_healthy_accepts_deterministic_valid_recommendations():
    oracle = _oracle()
    catalog = {"OLJCESPC7Z", "66VCHSJNUP"}
    frozen = _snapshot(
        succeeded=20,
        completed=20,
        product_ids=("OLJCESPC7Z", "66VCHSJNUP"),
        distinct_recommendation_sets=1,
    )
    assert oracle._wave_healthy(frozen, 0.1, catalog, concurrency=8) is True


def test_wave_rejects_exclusions_even_when_responses_vary():
    oracle = _oracle()
    catalog = {"OLJCESPC7Z", "66VCHSJNUP", "1YMWWN1N4O"}
    failure = oracle._wave_failure(
        _snapshot(distinct_recommendation_sets=20),
        1.0,
        catalog,
        concurrency=4,
        product_ids=("OLJCESPC7Z", "66VCHSJNUP"),
    )
    assert failure["reason"] == "invalid_recommendation_ids"
    assert failure["detail"]["excluded"] == ["66VCHSJNUP", "OLJCESPC7Z"]


def test_wave_healthy_rejects_slow_or_errorful_traffic():
    oracle = _oracle()
    catalog = {"OLJCESPC7Z", "66VCHSJNUP", "1YMWWN1N4O"}
    assert oracle._wave_healthy(_snapshot(success_rate=0.5), 1.0, catalog, concurrency=8) is False
    assert oracle._wave_healthy(_snapshot(p95_latency_seconds=9.0), 1.0, catalog, concurrency=8) is False
    assert oracle._wave_healthy(_snapshot(p99_latency_seconds=13.0), 1.0, catalog, concurrency=8) is False


def _named_deployment(name, **kwargs):
    deployment = _deployment(**kwargs)
    deployment.metadata.name = name
    return deployment


def test_resources_unchanged_rejects_replica_and_limit_cheats():
    oracle = _oracle()
    healthy = _named_deployment("recommendation")
    oracle._baseline_shape = {"recommendation": oracle._fingerprint_deployment(healthy)}
    oracle._baseline_replicas = {"recommendation": 1}

    def set_items(deployment):
        oracle.problem.kubectl.apps_v1_api.list_namespaced_deployment = Mock(
            return_value=SimpleNamespace(items=[deployment])
        )

    set_items(_named_deployment("recommendation", replicas=5))
    assert oracle._resources_unchanged() is False

    set_items(_named_deployment("recommendation", cpu_limit="2"))
    assert oracle._resources_unchanged() is False

    set_items(_named_deployment("recommendation"))
    assert oracle._resources_unchanged() is True


def test_capacity_guard_rejects_an_added_replacement_deployment():
    oracle = _oracle()
    original = [_named_deployment(name) for name in oracle.guarded_deployments]
    oracle.problem.kubectl.apps_v1_api.list_namespaced_deployment.return_value = SimpleNamespace(items=original)
    oracle.capture_baseline()
    oracle.problem.kubectl.apps_v1_api.list_namespaced_deployment.return_value = SimpleNamespace(
        items=[*original, _named_deployment("recommendation-replacement")]
    )

    failure = oracle._capacity_changed()

    assert failure["reason"] == "capacity_changed"
    assert failure["detail"]["added_deployments"] == ["recommendation-replacement"]


def test_run_wave_polls_until_both_rpc_counters_move(monkeypatch):
    oracle = _oracle()
    oracle.scrape_wait_seconds = 15.0
    oracle.poll_interval_seconds = 5.0
    oracle._catalog_list_products_total = Mock(side_effect=[100.0, 100.0, 340.0, 340.0])
    oracle._list_recommendations_total = Mock(side_effect=[10.0, 10.0, 10.0, 50.0])
    sleeps = []
    monkeypatch.setattr(
        "sregym.conductor.oracles.thundering_herd_mitigation.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )

    measured = oracle._run_wave(concurrency=8, product_ids=("OLJCESPC7Z",))

    assert measured is not None
    snapshot, amplification, _ = measured
    assert snapshot.succeeded == 40
    assert amplification == 6.0
    assert sleeps == [5.0, 5.0, 5.0]


def test_overlay_log_amplification_uses_refetch_ratio():
    oracle = _oracle()
    oracle.problem.kubectl.exec_command_checked = Mock(return_value="\n".join(["recommendation catalog refetch"] * 20))
    since_time = "2026-10-04T10:00:00Z"
    assert oracle._overlay_log_amplification(2, since_time=since_time) == 10.0
    command = oracle.problem.kubectl.exec_command_checked.call_args.args[0]
    assert f"--since-time={since_time}" in command
    assert "--since=5m" not in command
    assert "--tail=-1" in command


def test_overlay_log_fallback_keeps_the_same_wave_window():
    oracle = _oracle()
    oracle.problem.kubectl.exec_command_checked = Mock(side_effect=[RuntimeError("selector failed"), ""])
    since_time = "2026-10-04T10:00:00Z"
    assert oracle._overlay_log_amplification(40, since_time=since_time) == 0.0
    commands = [call.args[0] for call in oracle.problem.kubectl.exec_command_checked.call_args_list]
    assert all(f"--since-time={since_time}" in command for command in commands)
    assert "deploy/recommendation" in commands[1]


def test_overlay_log_falls_back_when_the_selector_matches_no_pods():
    oracle = _oracle()
    oracle.problem.kubectl.exec_command_checked = Mock(side_effect=["", "\n".join([oracle.overlay_log_marker] * 400)])
    assert oracle._overlay_log_amplification(40, since_time="2026-10-04T10:00:00Z") == 10.0


def test_run_wave_uses_rpc_ratio_when_http_success_count_differs(monkeypatch):
    oracle = _oracle()
    oracle._catalog_list_products_total = Mock(side_effect=[0.0, 100.0])
    oracle._list_recommendations_total = Mock(side_effect=[0.0, 10.0])
    monkeypatch.setattr("sregym.conductor.oracles.thundering_herd_mitigation.time.sleep", lambda _: None)

    measured = oracle._run_wave(concurrency=8, product_ids=("OLJCESPC7Z",))

    assert measured is not None
    _, amplification, _ = measured
    assert amplification == 10.0


def test_rpc_amplification_fails_closed_without_recommendation_spans():
    oracle = _oracle()

    assert oracle._rpc_amplification(100.0, 0.0) is None
    assert oracle._rpc_amplification(0.0, 0.0) is None


def test_rpc_amplification_fails_closed_on_counter_reset():
    oracle = _oracle()

    assert oracle._rpc_amplification(-100.0, 40.0) is None
    assert oracle._rpc_amplification(100.0, -40.0) is None


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
def test_rpc_amplification_fails_closed_for_nonfinite_counters(invalid):
    oracle = _oracle()
    assert oracle._rpc_amplification(invalid, 40.0) is None
    assert oracle._rpc_amplification(0.0, invalid) is None


def test_fault_verification_can_use_logs_when_rpc_ratio_is_incomplete(monkeypatch):
    oracle = _oracle()
    oracle.scrape_wait_seconds = 5.0
    oracle.poll_interval_seconds = 5.0
    oracle._catalog_list_products_total = Mock(side_effect=[0.0, 100.0])
    oracle._list_recommendations_total = Mock(side_effect=[0.0, 0.0])
    oracle.problem.kubectl.exec_command_checked = Mock(return_value="\n".join([oracle.overlay_log_marker] * 400))
    monkeypatch.setattr(
        "sregym.conductor.oracles.thundering_herd_mitigation.time.sleep",
        lambda _: None,
    )

    oracle.assert_fault_present()


def test_fault_verification_initializes_cold_rpc_series(monkeypatch):
    oracle = _oracle()
    oracle.scrape_wait_seconds = 10.0
    oracle._catalog_list_products_total = Mock(side_effect=[None, None, 400.0])
    oracle._list_recommendations_total = Mock(side_effect=[None, None, 40.0])
    oracle.problem.kubectl.exec_command_checked = Mock(return_value="\n".join([oracle.overlay_log_marker] * 400))
    monkeypatch.setattr("sregym.conductor.oracles.thundering_herd_mitigation.time.sleep", lambda _: None)

    oracle.assert_fault_present()

    oracle.problem.workload.run.assert_called_once()


def test_fault_verification_rejects_when_metrics_and_logs_are_unavailable(monkeypatch):
    oracle = _oracle()
    oracle._catalog_list_products_total = Mock(return_value=None)
    oracle._list_recommendations_total = Mock(return_value=None)
    monkeypatch.setattr("sregym.conductor.oracles.thundering_herd_mitigation.time.sleep", lambda _: None)

    with pytest.raises(RuntimeError, match="metrics were not available"):
        oracle.assert_fault_present()


def test_evaluate_passes_both_waves_and_stops_workload(monkeypatch):
    oracle = _oracle()
    oracle._cluster_shape_unhealthy = Mock(return_value=None)
    oracle._capacity_changed = Mock(return_value=None)
    oracle._catalog_list_products_total = Mock(side_effect=[99.0, 100.0, 100.0, 140.0, 140.0, 180.0])
    oracle._list_recommendations_total = Mock(side_effect=[50.0, 90.0, 90.0, 130.0])
    monkeypatch.setattr("sregym.conductor.oracles.thundering_herd_mitigation.time.sleep", lambda _: None)

    result = oracle.evaluate()

    assert result == {"success": True}
    assert oracle.problem.workload.run.call_count == 2
    first_kwargs = oracle.problem.workload.run.call_args_list[0].kwargs
    second_kwargs = oracle.problem.workload.run.call_args_list[1].kwargs
    assert first_kwargs["concurrency"] == ThunderingHerdMitigationOracle.visible_concurrency
    assert second_kwargs["concurrency"] == ThunderingHerdMitigationOracle.hidden_concurrency
    first_returned = oracle.problem.workload.catalog_product_ids() - set(first_kwargs["product_ids"])
    assert set(second_kwargs["product_ids"]) == first_returned
    oracle.problem.workload.stop.assert_called()


def test_evaluate_fails_closed_when_prometheus_is_empty(monkeypatch):
    oracle = _oracle()
    oracle._cluster_shape_unhealthy = Mock(return_value=None)
    oracle._capacity_changed = Mock(return_value=None)
    oracle._catalog_list_products_total = Mock(return_value=None)
    oracle._list_recommendations_total = Mock(return_value=None)
    monkeypatch.setattr("sregym.conductor.oracles.thundering_herd_mitigation.time.sleep", lambda _: None)

    result = oracle.evaluate()
    assert result["success"] is False
    assert result["reason"] == "prometheus_unreachable"
    oracle.problem.workload.stop.assert_called()


@pytest.mark.parametrize("failed_wave", ["visible", "hidden"])
@pytest.mark.parametrize("product_delta,recommendation_delta", [(40.0, 0.0), (-1.0, 40.0), (40.0, -1.0)])
def test_evaluate_reports_stalled_rpc_telemetry_for_invalid_deltas(
    monkeypatch, failed_wave, product_delta, recommendation_delta
):
    oracle = _oracle()
    oracle._cluster_shape_unhealthy = Mock(return_value=None)
    oracle._capacity_changed = Mock(return_value=None)
    if failed_wave == "visible":
        product_samples = [100.0, 101.0, 101.0, 101.0 + product_delta]
        recommendation_samples = [100.0, 100.0 + recommendation_delta]
    else:
        product_samples = [100.0, 101.0, 101.0, 141.0, 141.0, 141.0 + product_delta]
        recommendation_samples = [100.0, 140.0, 140.0, 140.0 + recommendation_delta]
    oracle._catalog_list_products_total = Mock(side_effect=product_samples)
    oracle._list_recommendations_total = Mock(side_effect=recommendation_samples)
    monkeypatch.setattr("sregym.conductor.oracles.thundering_herd_mitigation.time.sleep", lambda _: None)

    result = oracle.evaluate()

    assert result["reason"] == "rpc_telemetry_stalled"
    assert result["failure_class"] == "environment_error"
    assert result["detail"]["wave"] == failed_wave


@pytest.mark.parametrize("failed_wave", ["visible", "hidden"])
def test_evaluate_reports_missing_metric_samples_as_prometheus_unreachable(monkeypatch, failed_wave):
    oracle = _oracle()
    oracle._cluster_shape_unhealthy = Mock(return_value=None)
    oracle._capacity_changed = Mock(return_value=None)
    oracle._catalog_list_products_total = Mock(
        side_effect=[100.0, 101.0, 101.0, None]
        if failed_wave == "visible"
        else [100.0, 101.0, 101.0, 141.0, 141.0, None]
    )
    oracle._list_recommendations_total = Mock(
        side_effect=[100.0, None] if failed_wave == "visible" else [100.0, 140.0, 140.0, None]
    )
    monkeypatch.setattr("sregym.conductor.oracles.thundering_herd_mitigation.time.sleep", lambda _: None)

    result = oracle.evaluate()

    assert result["success"] is False
    assert result["reason"] == "prometheus_unreachable"


@pytest.mark.parametrize("after_probe", [100.0, 0.0])
def test_evaluate_rejects_stalled_or_reset_catalog_telemetry_before_waves(monkeypatch, after_probe):
    oracle = _oracle()
    oracle._cluster_shape_unhealthy = Mock(return_value=None)
    oracle._capacity_changed = Mock(return_value=None)
    oracle._catalog_list_products_total = Mock(side_effect=[100.0, after_probe])
    # Advancing recommendation spans must not make missing catalog traffic
    # look like a correctly cached repair.
    oracle._list_recommendations_total = Mock(side_effect=[50.0, 90.0, 90.0, 130.0])
    monkeypatch.setattr("sregym.conductor.oracles.thundering_herd_mitigation.time.sleep", lambda _: None)

    result = oracle.evaluate()

    assert result["reason"] == "rpc_telemetry_stalled"
    assert result["failure_class"] == "environment_error"
    assert result["detail"]["service"] == "product-catalog"
    oracle.problem.workload.catalog_product_ids.assert_called_once_with()
    oracle.problem.workload.run.assert_not_called()


def test_evaluate_rejects_high_amplification_even_when_latency_is_fine(monkeypatch):
    oracle = _oracle()
    oracle._cluster_shape_unhealthy = Mock(return_value=None)
    oracle._capacity_changed = Mock(return_value=None)
    oracle._catalog_list_products_total = Mock(side_effect=[0.0, 1.0, 1.0, 401.0])
    oracle._list_recommendations_total = Mock(side_effect=[0.0, 40.0])
    monkeypatch.setattr("sregym.conductor.oracles.thundering_herd_mitigation.time.sleep", lambda _: None)

    result = oracle.evaluate()
    assert result["success"] is False
    assert result["reason"] == "fault_still_present"


def test_evaluate_rejects_fixed_responder_with_dynamic_exclusions(monkeypatch):
    oracle = _oracle()
    fixed_ids = tuple(f"fixed-{index}" for index in range(5))
    catalog = {*fixed_ids, *oracle.seed_product_ids, *oracle.hidden_product_ids}
    oracle.problem.workload.catalog_product_ids.return_value = catalog
    oracle.problem.workload.run.side_effect = None
    oracle.problem.workload.run.return_value = _snapshot(product_ids=fixed_ids, distinct_recommendation_sets=20)
    oracle._cluster_shape_unhealthy = Mock(return_value=None)
    oracle._capacity_changed = Mock(return_value=None)
    oracle._catalog_list_products_total = Mock(side_effect=[0.0, 1.0, 1.0, 41.0, 41.0, 81.0])
    oracle._list_recommendations_total = Mock(side_effect=[0.0, 40.0, 40.0, 80.0])
    monkeypatch.setattr("sregym.conductor.oracles.thundering_herd_mitigation.time.sleep", lambda _: None)

    result = oracle.evaluate()

    assert result["reason"] == "invalid_recommendation_ids"
    assert set(result["detail"]["excluded"]) == set(fixed_ids)
    second_exclusions = oracle.problem.workload.run.call_args_list[1].kwargs["product_ids"]
    assert set(fixed_ids).issubset(second_exclusions)
    assert catalog.difference(second_exclusions)


def test_evaluate_passes_with_no_new_catalog_calls(monkeypatch):
    oracle = _oracle()
    oracle._cluster_shape_unhealthy = Mock(return_value=None)
    oracle._capacity_changed = Mock(return_value=None)
    oracle._catalog_list_products_total = Mock(side_effect=[99.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 101.0])
    oracle._list_recommendations_total = Mock(side_effect=[50.0, 90.0, 90.0, 130.0])
    monkeypatch.setattr("sregym.conductor.oracles.thundering_herd_mitigation.time.sleep", lambda _: None)

    assert oracle.evaluate() == {"success": True}
    assert oracle.problem.workload.catalog_product_ids.call_count == 2


def test_evaluate_rechecks_catalog_telemetry_after_zero_call_waves(monkeypatch):
    oracle = _oracle()
    oracle._cluster_shape_unhealthy = Mock(return_value=None)
    oracle._capacity_changed = Mock(return_value=None)
    # The preflight succeeds, then catalog spans stop while recommendation
    # spans keep advancing. A final real lookup exposes the stalled exporter.
    oracle._catalog_list_products_total = Mock(side_effect=[99.0, *([100.0] * 7)])
    oracle._list_recommendations_total = Mock(side_effect=[50.0, 90.0, 90.0, 130.0])
    monkeypatch.setattr("sregym.conductor.oracles.thundering_herd_mitigation.time.sleep", lambda _: None)

    result = oracle.evaluate()

    assert result["reason"] == "rpc_telemetry_stalled"
    assert oracle.problem.workload.run.call_count == 2
    assert oracle.problem.workload.catalog_product_ids.call_count == 2


def test_evaluate_grades_real_cached_rpcs_despite_retained_debug_messages(monkeypatch):
    oracle = _oracle()
    oracle._cluster_shape_unhealthy = Mock(return_value=None)
    oracle._capacity_changed = Mock(return_value=None)
    oracle._catalog_list_products_total = Mock(side_effect=[99.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 101.0])
    oracle._list_recommendations_total = Mock(side_effect=[50.0, 90.0, 90.0, 130.0])
    oracle.problem.kubectl.exec_command_checked = Mock(return_value="\n".join([oracle.overlay_log_marker] * 400))
    monkeypatch.setattr("sregym.conductor.oracles.thundering_herd_mitigation.time.sleep", lambda _: None)

    assert oracle.evaluate() == {"success": True}


def test_run_wave_waits_for_delayed_catalog_export(monkeypatch):
    oracle = _oracle()
    oracle.scrape_wait_seconds = 15.0
    oracle._catalog_list_products_total = Mock(side_effect=[0.0, 40.0, 200.0, 400.0])
    oracle._list_recommendations_total = Mock(side_effect=[0.0, 40.0, 40.0, 40.0])
    monkeypatch.setattr("sregym.conductor.oracles.thundering_herd_mitigation.time.sleep", lambda _: None)

    _, amplification, _ = oracle._run_wave(concurrency=4, product_ids=oracle.seed_product_ids)

    assert amplification == 10.0


def test_evaluate_ignores_historical_refetch_logs(monkeypatch):
    oracle = _oracle()
    oracle._cluster_shape_unhealthy = Mock(return_value=None)
    oracle._capacity_changed = Mock(return_value=None)
    oracle._catalog_list_products_total = Mock(side_effect=[999.0, 1000.0, 1000.0, 1040.0, 1040.0, 1080.0])
    oracle._list_recommendations_total = Mock(side_effect=[100.0, 140.0, 140.0, 180.0])
    # A five-minute query would include 360 historical calls; a wave query
    # includes only the current one-call repair's 40 calls.
    oracle.problem.kubectl.exec_command_checked = Mock(
        side_effect=lambda command, **kwargs: "\n".join(
            [oracle.overlay_log_marker] * (40 if "--since-time=" in command else 400)
        )
    )
    monkeypatch.setattr("sregym.conductor.oracles.thundering_herd_mitigation.time.sleep", lambda _: None)

    assert oracle.evaluate() == {"success": True}


@pytest.mark.parametrize("healthy", [True, False])
def test_evaluate_pauses_and_resumes_investigation_traffic(monkeypatch, healthy):
    oracle = _oracle()
    oracle.problem.workload.background_running = True
    events = []
    oracle.problem.workload.stop_background.side_effect = lambda: events.append("drain")
    oracle.problem.start_workload.side_effect = lambda: events.append("resume")
    oracle._cluster_shape_unhealthy = Mock(return_value=None)
    oracle._capacity_changed = Mock(return_value=None)
    oracle._catalog_list_products_total = Mock(side_effect=[0.0, 1.0, 1.0, 41.0, 41.0, 81.0] if healthy else [None])
    oracle._list_recommendations_total = Mock(side_effect=[0.0, 40.0, 40.0, 80.0] if healthy else [None])
    monkeypatch.setattr(
        "sregym.conductor.oracles.thundering_herd_mitigation.time.sleep",
        lambda _: events.append("settle"),
    )

    assert oracle.evaluate()["success"] is healthy
    assert events[0:2] == ["drain", "settle"]
    assert events[-1] == "resume"
    oracle.problem.workload.stop.assert_not_called()
