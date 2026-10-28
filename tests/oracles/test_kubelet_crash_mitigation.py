from collections import Counter
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.kubelet_crash_mitigation import KubeletCrashMitigationOracle


def deployment(name="frontend"):
    return NS(
        metadata=NS(name=name, uid=name, generation=2, deletion_timestamp=None),
        spec=NS(replicas=1),
        status=NS(
            observed_generation=2,
            replicas=1,
            updated_replicas=1,
            ready_replicas=1,
            available_replicas=1,
            unavailable_replicas=0,
        ),
    )


def pod(name="frontend-current", node="healthy"):
    return NS(
        metadata=NS(name=name, uid=name, deletion_timestamp=None),
        spec=NS(node_name=node),
        status=NS(
            phase="Running", container_statuses=[NS(ready=True, name="app", state=NS(waiting=None, terminated=None))]
        ),
    )


def oracle(monkeypatch):
    kubectl = Mock()
    kubectl.list_nodes.return_value.items = [
        NS(metadata=NS(name="healthy"), status=NS(conditions=[NS(type="Ready", status="True")]))
    ]
    check = KubeletCrashMitigationOracle(NS(namespace="astronomy-shop", kubectl=kubectl))
    check.deployments = {"frontend"}
    check.services = {"frontend": {"frontend"}}
    check.records = {"orders": Counter(original=1)}
    check.cart = {"items": [{"productId": check.product_id, "quantity": 2}]}
    check.observation_seconds = 0
    check.rollout_seconds = 0
    monkeypatch.setattr("sregym.conductor.oracles.kubelet_crash_mitigation.time.sleep", lambda _: None)
    check._topology = Mock(
        return_value=(
            {"frontend": deployment()},
            {"frontend-current": pod()},
            {"frontend-current": "frontend"},
            {"frontend": [NS(target_ref=NS(uid="frontend-current", kind="Pod"))]},
        )
    )
    check._records = Mock(return_value={"orders": Counter(original=1, new=1)})
    check._cart = Mock(return_value=check.cart)
    check._shop = Mock(return_value={"id": check.product_id, "priceUsd": {"currencyCode": "EUR"}})
    check._checkout = Mock(side_effect=["first-new-order", "second-new-order"])
    check._workload = Mock(side_effect=[Counter(requests=100, failures=50), Counter(requests=200, failures=50)])
    check._observe_workload = Mock(return_value=(Counter(requests=100, failures=0), 5))
    return check


def test_accepts_application_recovery_with_existing_outage_errors(monkeypatch):
    check = oracle(monkeypatch)
    result = check.evaluate()
    assert result["success"]
    assert result["detail"]["requests"] == 100
    assert result["detail"]["failures"] == 0


@pytest.mark.parametrize(
    "change,reason",
    [
        ("deleted", "required_deployment_missing"),
        ("zero", "required_deployment_scaled_to_zero"),
        ("old_generation", "deployment_replicas_unready"),
        ("old_replicas", "deployment_replicas_unready"),
        ("failed_node", "active_application_pods_unready"),
        ("not_ready", "active_application_pods_unready"),
        ("missing_endpoint", "no_ready_endpoints"),
        ("fake_endpoint", "unexpected_or_unready_endpoint"),
    ],
)
def test_rejects_incomplete_active_serving_state(monkeypatch, change, reason):
    check = oracle(monkeypatch)
    deployments, pods, owners, endpoints = check._topology.return_value
    if change == "deleted":
        deployments.clear()
    elif change == "zero":
        deployments["frontend"].spec.replicas = 0
    elif change == "old_generation":
        deployments["frontend"].status.observed_generation = 1
    elif change == "old_replicas":
        deployments["frontend"].status.updated_replicas = 0
    elif change == "failed_node":
        pods["frontend-current"].spec.node_name = "failed-worker"
    elif change == "not_ready":
        pods["frontend-current"].status.container_statuses[0].ready = False
    elif change == "missing_endpoint":
        endpoints.clear()
    elif change == "fake_endpoint":
        pods["unrelated"] = pod("unrelated")
        endpoints["frontend"][0].target_ref.uid = "unrelated"
    assert check.evaluate()["reason"] == reason


def test_retired_instances_do_not_override_current_serving_state(monkeypatch):
    check = oracle(monkeypatch)
    _, pods, _, _ = check._topology.return_value
    pods["frontend-retired"] = pod("frontend-retired", node="failed-worker")
    pods["frontend-retired"].status.phase = "Unknown"
    assert check.evaluate()["success"]


@pytest.mark.parametrize("records", [Counter(), Counter(replacement=1)])
def test_rejects_lost_or_replaced_original_records(monkeypatch, records):
    check = oracle(monkeypatch)
    check._records.return_value = {"orders": records}
    assert check.evaluate()["reason"] == "application_data_changed"


def test_rechecks_data_after_observation(monkeypatch):
    check = oracle(monkeypatch)
    check._records.side_effect = [{"orders": Counter(original=1)}, {"orders": Counter()}]
    assert check.evaluate()["reason"] == "application_data_changed"


def test_rejects_lost_saved_cart(monkeypatch):
    check = oracle(monkeypatch)
    check._cart.return_value = {"items": []}
    assert check.evaluate()["reason"] == "application_cart_changed"


def test_rejects_fake_product_response(monkeypatch):
    check = oracle(monkeypatch)
    check._shop.return_value = {"id": "other-product", "priceUsd": {"currencyCode": "EUR"}}
    assert check.evaluate()["reason"] == "application_response_invalid"


@pytest.mark.parametrize(
    "measurement",
    [
        (Counter(), 0),
        (Counter(requests=0, failures=0), 5),
        (Counter(requests=100, failures=25), 5),
        (Counter(requests=200, failures=0), 1),
    ],
)
def test_rejects_no_traffic_new_errors_and_missing_intervals(monkeypatch, measurement):
    check = oracle(monkeypatch)
    check._observe_workload.return_value = measurement
    assert check.evaluate()["reason"] == "application_workload_unhealthy"


def test_missing_measurement_does_not_pass(monkeypatch):
    check = oracle(monkeypatch)
    check._observe_workload.side_effect = ValueError("No workload measurement")
    assert not check.evaluate()["success"]


def test_api_failure_does_not_pass(monkeypatch):
    check = oracle(monkeypatch)
    check._topology.side_effect = ApiException(status=503)
    result = check.evaluate()
    assert result["reason"] == "kubernetes_api_error"
    assert result["failure_class"] == "environment_error"


def test_missing_baseline_does_not_pass(monkeypatch):
    check = oracle(monkeypatch)
    check.records = None
    assert check.evaluate()["reason"] == "oracle_baseline_missing"


def test_sql_failure_is_not_reported_as_data_loss(monkeypatch):
    import subprocess

    check = oracle(monkeypatch)
    check._records.side_effect = subprocess.CalledProcessError(1, ["kubectl", "exec"])
    assert check.evaluate()["reason"] == "oracle_command_failed"


def test_observation_discards_resets_but_keeps_fresh_errors(monkeypatch):
    check = oracle(monkeypatch)
    check.observation_seconds = 10
    check._workload.side_effect = [
        Counter(requests=100, failures=50),
        Counter(requests=110, failures=50),
        Counter(requests=2, failures=0),
        Counter(requests=12, failures=1),
    ]
    monkeypatch.setattr(
        "sregym.conductor.oracles.kubelet_crash_mitigation.time.monotonic", Mock(side_effect=[0, 0, 2, 4, 10])
    )
    totals, intervals = KubeletCrashMitigationOracle._observe_workload(check)
    assert totals == Counter(requests=20, failures=1)
    assert intervals == 2


def test_checkout_rejects_replayed_success(monkeypatch):
    import json

    check = oracle(monkeypatch)
    order_id = "11ab3266-9498-48a5-b3d6-dc0f32a97947"
    check._sql = Mock(return_value=json.dumps([order_id]))
    check._add_item = Mock()
    check._shop.return_value = {"orderId": order_id}
    with pytest.raises(ValueError, match="existing order"):
        KubeletCrashMitigationOracle._checkout(check)


def test_checkout_requires_a_real_new_order_with_items_and_shipping(monkeypatch):
    check = oracle(monkeypatch)
    order_id = "11ab3266-9498-48a5-b3d6-dc0f32a97947"
    check._sql = Mock(side_effect=["[]", "1"])
    check._add_item = Mock()
    check._shop.return_value = {
        "orderId": order_id,
        "items": [{"item": {"productId": check.product_id, "quantity": 2}}],
    }
    assert KubeletCrashMitigationOracle._checkout(check) == order_id
    assert "JOIN accounting.shipping" in check._sql.call_args.args[0]


def test_reference_recovery_does_not_restart_ephemeral_databases(monkeypatch):
    from sregym.conductor.problems.kubelet_crash import KubeletCrash

    problem = KubeletCrash.__new__(KubeletCrash)
    problem.injector = Mock()
    problem.kubectl = Mock()
    problem.namespace = "astronomy-shop"
    problem.recover_fault()
    problem.injector.recover_kubelet_crash.assert_called_once_with()
    problem.kubectl.wait_for_ready.assert_called_once_with("astronomy-shop")
    problem.kubectl.exec_command.assert_not_called()


def test_topology_follows_current_deployment_and_replicaset_uids(monkeypatch):
    check = oracle(monkeypatch)
    dep = deployment()
    rs = NS(
        metadata=NS(uid="current-rs", owner_references=[NS(kind="Deployment", name="frontend", uid=dep.metadata.uid)]),
        spec=NS(replicas=1),
    )
    inactive = NS(metadata=NS(uid="inactive-rs", owner_references=rs.metadata.owner_references), spec=NS(replicas=0))
    previous_deployment = NS(
        metadata=NS(
            uid="foreign-rs", owner_references=[NS(kind="Deployment", name="frontend", uid="deleted-deployment-uid")]
        ),
        spec=NS(replicas=1),
    )
    pods = [pod("current"), pod("retired"), pod("foreign")]
    for item, owner in zip(pods, [rs, inactive, previous_deployment], strict=True):
        item.metadata.owner_references = [NS(kind="ReplicaSet", uid=owner.metadata.uid)]
    kubectl = check.problem.kubectl
    kubectl.list_deployments.return_value.items = [dep]
    kubectl.apps_v1_api.list_namespaced_replica_set.return_value.items = [rs, inactive, previous_deployment]
    kubectl.list_pods.return_value.items = pods
    kubectl.core_v1_api.list_namespaced_endpoints.return_value.items = []
    _, _, owners, _ = KubeletCrashMitigationOracle._topology(check)
    assert owners == {"current": "frontend"}
