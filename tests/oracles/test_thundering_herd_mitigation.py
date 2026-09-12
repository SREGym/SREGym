from types import SimpleNamespace
from unittest.mock import Mock

from sregym.conductor.oracles.thundering_herd_mitigation import ThunderingHerdMitigationOracle
from sregym.generators.workload.recommendation_herd import HerdSnapshot


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
    workload = SimpleNamespace(
        start=Mock(),
        stop=Mock(),
        catalog_product_ids=Mock(return_value={"OLJCESPC7Z", "66VCHSJNUP", "1YMWWN1N4O", "HQTGWGPNH4"}),
        run=Mock(return_value=_snapshot()),
    )
    kubectl = SimpleNamespace(
        apps_v1_api=SimpleNamespace(list_namespaced_deployment=Mock()),
        core_v1_api=SimpleNamespace(read_namespaced_endpoints=Mock()),
    )
    problem = SimpleNamespace(namespace="astronomy-shop", kubectl=kubectl, workload=workload)
    return ThunderingHerdMitigationOracle(problem)


def test_rollout_requires_current_ready_nonzero_replicas():
    assert ThunderingHerdMitigationOracle._rollout_complete(_deployment()) is True
    assert ThunderingHerdMitigationOracle._rollout_complete(_deployment(replicas=0, ready=0)) is False
    assert ThunderingHerdMitigationOracle._rollout_complete(_deployment(observed=1)) is False
    assert ThunderingHerdMitigationOracle._rollout_complete(_deployment(ready=0)) is False


def test_wave_healthy_accepts_low_amplification_and_real_ids():
    oracle = _oracle()
    catalog = {"OLJCESPC7Z", "66VCHSJNUP", "1YMWWN1N4O", "HQTGWGPNH4"}
    assert oracle._wave_healthy(_snapshot(), 1.2, catalog, concurrency=8) is True


def test_wave_healthy_rejects_high_amplification():
    oracle = _oracle()
    catalog = {"OLJCESPC7Z", "66VCHSJNUP", "1YMWWN1N4O"}
    assert oracle._wave_healthy(_snapshot(), 9.5, catalog, concurrency=8) is False


def test_wave_healthy_rejects_zero_catalog_increase():
    oracle = _oracle()
    catalog = {"OLJCESPC7Z", "66VCHSJNUP", "1YMWWN1N4O"}
    assert oracle._wave_healthy(_snapshot(), 0.0, catalog, concurrency=8) is False


def test_wave_healthy_rejects_empty_and_unknown_ids():
    oracle = _oracle()
    catalog = {"OLJCESPC7Z"}
    assert oracle._wave_healthy(_snapshot(product_ids=()), 1.0, catalog, concurrency=8) is False
    assert (
        oracle._wave_healthy(_snapshot(product_ids=("not-a-product",)), 1.0, catalog, concurrency=8)
        is False
    )


def test_wave_healthy_rejects_frozen_hard_coded_ids():
    oracle = _oracle()
    catalog = {"OLJCESPC7Z", "66VCHSJNUP"}
    frozen = _snapshot(
        succeeded=20,
        completed=20,
        product_ids=("OLJCESPC7Z", "66VCHSJNUP"),
        distinct_recommendation_sets=1,
    )
    assert oracle._wave_healthy(frozen, 0.1, catalog, concurrency=8) is False


def test_wave_healthy_rejects_slow_or_errorful_traffic():
    oracle = _oracle()
    catalog = {"OLJCESPC7Z", "66VCHSJNUP", "1YMWWN1N4O"}
    assert oracle._wave_healthy(_snapshot(success_rate=0.5), 1.0, catalog, concurrency=8) is False
    assert oracle._wave_healthy(_snapshot(p95_latency_seconds=9.0), 1.0, catalog, concurrency=8) is False
    assert oracle._wave_healthy(_snapshot(p99_latency_seconds=12.0), 1.0, catalog, concurrency=8) is False


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


def test_run_wave_polls_until_catalog_counter_moves(monkeypatch):
    oracle = _oracle()
    oracle.scrape_wait_seconds = 15.0
    oracle.poll_interval_seconds = 5.0
    oracle._catalog_list_products_total = Mock(side_effect=[100.0, 100.0, 100.0, 340.0])
    oracle._list_recommendations_total = Mock(side_effect=[10.0, 10.0, 10.0, 50.0])
    sleeps = []
    monkeypatch.setattr(
        "sregym.conductor.oracles.thundering_herd_mitigation.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )

    measured = oracle._run_wave(concurrency=8, product_ids=("OLJCESPC7Z",))

    assert measured is not None
    snapshot, amplification = measured
    assert snapshot.succeeded == 40
    assert amplification == 6.0
    assert sleeps == [5.0, 5.0, 5.0]


def test_run_wave_uses_rpc_ratio_when_http_is_cached(monkeypatch):
    oracle = _oracle()
    oracle._catalog_list_products_total = Mock(side_effect=[0.0, 100.0])
    oracle._list_recommendations_total = Mock(side_effect=[0.0, 10.0])
    monkeypatch.setattr("sregym.conductor.oracles.thundering_herd_mitigation.time.sleep", lambda _: None)

    measured = oracle._run_wave(concurrency=8, product_ids=("OLJCESPC7Z",))

    assert measured is not None
    _, amplification = measured
    assert amplification == 10.0


def test_evaluate_passes_both_waves_and_stops_workload(monkeypatch):
    oracle = _oracle()
    oracle._cluster_shape_unhealthy = Mock(return_value=None)
    oracle._capacity_changed = Mock(return_value=None)
    oracle._catalog_list_products_total = Mock(side_effect=[100.0, 140.0, 140.0, 180.0])
    oracle._list_recommendations_total = Mock(side_effect=[50.0, 90.0, 90.0, 130.0])
    monkeypatch.setattr("sregym.conductor.oracles.thundering_herd_mitigation.time.sleep", lambda _: None)

    result = oracle.evaluate()

    assert result == {"success": True}
    assert oracle.problem.workload.run.call_count == 2
    first_kwargs = oracle.problem.workload.run.call_args_list[0].kwargs
    second_kwargs = oracle.problem.workload.run.call_args_list[1].kwargs
    assert first_kwargs["concurrency"] == ThunderingHerdMitigationOracle.visible_concurrency
    assert second_kwargs["concurrency"] == ThunderingHerdMitigationOracle.hidden_concurrency
    assert second_kwargs["product_ids"] == ThunderingHerdMitigationOracle.hidden_product_ids
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


def test_evaluate_rejects_high_amplification_even_when_latency_is_fine(monkeypatch):
    oracle = _oracle()
    oracle._cluster_shape_unhealthy = Mock(return_value=None)
    oracle._capacity_changed = Mock(return_value=None)
    oracle._catalog_list_products_total = Mock(side_effect=[0.0, 400.0])
    oracle._list_recommendations_total = Mock(side_effect=[0.0, 40.0])
    monkeypatch.setattr("sregym.conductor.oracles.thundering_herd_mitigation.time.sleep", lambda _: None)

    result = oracle.evaluate()
    assert result["success"] is False
    assert result["reason"] == "fault_still_present"
