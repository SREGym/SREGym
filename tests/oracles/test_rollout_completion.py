"""The same rollout contract must hold for every Deployment-based oracle."""

from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes import client

from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.service.rollout import deployment_rollout_complete

ROLLOUT_CHECKS = [
    ("admission_webhook_outage_mitigation", "AdmissionWebhookOutageMitigationOracle"),
    ("mutating_webhook_resource_limits_mitigation", "MutatingWebhookResourceLimitsMitigationOracle"),
    ("readiness_probe_mitigation", "ReadinessProbeMitigationOracle"),
    ("service_endpoint_mitigation", "ServiceEndpointMitigationOracle"),
    ("incorrect_port", "IncorrectPortAssignmentMitigationOracle"),
    ("cpu_throttling_mitigation", "CpuThrottlingMitigationOracle"),
    ("dns_resolution_mitigation", "DNSResolutionMitigationOracle"),
    ("wrong_pod_selection_mitigation", "WrongPodSelectionMitigationOracle"),
    ("rolling_update_misconfiguration_mitigation", "RollingUpdateMitigationOracle"),
    ("env_variable_shadowing_mitigation", "EnvVariableShadowingMitigationOracle"),
    ("secret_rotation_stale_env_mitigation", "SecretRotationStaleEnvMitigation"),
    ("namespace_memory_limit_mitigation", "NamespaceMemoryLimitMitigationOracle"),
    ("network_policy_oracle", "NetworkPolicyMitigationOracle"),
    ("duplicate_pvc_mounts_mitigation", "DuplicatePVCMountsMitigationOracle"),
    ("edge_request_filter_mitigation", "EdgeRequestFilterMitigationOracle"),
    ("search_rate_retry_mitigation", "SearchRateRetryMitigationOracle"),
]


def deployment(**status):
    fields = dict(
        observed_generation=2,
        replicas=1,
        updated_replicas=1,
        ready_replicas=1,
        available_replicas=1,
        unavailable_replicas=0,
    )
    fields.update(status)
    return client.V1Deployment(
        metadata=client.V1ObjectMeta(name="frontend", generation=2),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels={"app": "frontend"}),
            template=client.V1PodTemplateSpec(),
        ),
        status=client.V1DeploymentStatus(**fields),
    )


@pytest.mark.parametrize("module_name,class_name", ROLLOUT_CHECKS)
@pytest.mark.parametrize("total,expected", [(1, True), (2, False)])
def test_all_oracles_reject_extra_old_replicas(module_name, class_name, total, expected):
    oracle = getattr(import_module(f"sregym.conductor.oracles.{module_name}"), class_name)
    dep = deployment(replicas=total)
    if module_name == "rolling_update_misconfiguration_mitigation":
        dep = client.ApiClient().sanitize_for_serialization(dep)
    assert oracle._rollout_complete(dep) is expected


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize(
    "status",
    [
        {"replicas": 2},
        {"replicas": 0},
        {"replicas": None},
        {"observed_generation": 1},
        {"observed_generation": None},
        {"updated_replicas": 0},
        {"ready_replicas": 0},
        {"ready_replicas": 2},
        {"available_replicas": 0},
        {"unavailable_replicas": 1},
    ],
)
def test_shared_predicate_rejects_incomplete_status(status, as_json):
    dep = deployment(**status)
    if as_json:
        dep = client.ApiClient().sanitize_for_serialization(dep)
    assert not deployment_rollout_complete(dep)


@pytest.mark.parametrize("as_json", [False, True])
def test_scale_to_zero_requires_explicit_setup_or_recovery_opt_in(as_json):
    dep = deployment(replicas=0, updated_replicas=0, ready_replicas=0, available_replicas=0)
    dep.spec.replicas = 0
    if as_json:
        dep = client.ApiClient().sanitize_for_serialization(dep)
    assert not deployment_rollout_complete(dep)
    assert deployment_rollout_complete(dep, allow_zero=True)


@pytest.mark.parametrize("replicas", [None, 1, 3])
def test_default_and_scaled_healthy_deployments(replicas):
    count = 1 if replicas is None else replicas
    dep = deployment(replicas=count, updated_replicas=count, ready_replicas=count, available_replicas=count)
    dep.spec.replicas = replicas
    assert deployment_rollout_complete(dep)


@pytest.mark.parametrize("field", ["metadata", "spec", "status"])
def test_missing_resources_and_status_fail_closed(field):
    dep = client.ApiClient().sanitize_for_serialization(deployment())
    dep[field] = None
    assert not deployment_rollout_complete(dep)
    assert not deployment_rollout_complete(None)
    assert not deployment_rollout_complete({})


def test_deleting_deployment_is_not_a_completed_rollout():
    dep = client.ApiClient().sanitize_for_serialization(deployment())
    dep["metadata"]["deletionTimestamp"] = "2026-09-09T00:00:00Z"
    assert not deployment_rollout_complete(dep)


@pytest.mark.parametrize("baseline", [True, False])
@pytest.mark.parametrize("status", [{"replicas": 2}, {"updated_replicas": 0}, {"observed_generation": 1}, None])
def test_generic_final_check_rejects_stale_rollout_after_settle_timeout(baseline, status):
    dep = deployment(**(status or {}))
    if status is None:
        dep.status = None
    kube = Mock()
    kube.list_deployments.return_value = SimpleNamespace(items=[dep])
    oracle = MitigationOracle(SimpleNamespace(kubectl=kube, namespace="example"))
    oracle.rollout_time = 0
    oracle.replica_count = {"frontend": 1} if baseline else {}
    result = oracle.evaluate()
    assert result["success"] is False
    assert result["reason"] == "deployment_replicas_unready"
    kube.list_pods.assert_not_called()


def test_generic_wait_rechecks_until_rollout_finishes(monkeypatch):
    import sregym.conductor.oracles.mitigation as module

    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    kube = Mock()
    kube.list_deployments.side_effect = [
        SimpleNamespace(items=[deployment(replicas=2)]),
        SimpleNamespace(items=[deployment()]),
    ]
    oracle = MitigationOracle(SimpleNamespace(kubectl=kube, namespace="example"))
    oracle._wait_for_rollouts(kube, "example")
    assert kube.list_deployments.call_count == 2


@pytest.mark.parametrize("total,expected", [(1, True), (2, False)])
def test_non_lite_status_checks_use_the_same_contract(total, expected):
    from sregym.conductor.oracles.calico_route_reflector_mitigation import CalicoRouteReflectorMitigationOracle
    from sregym.conductor.oracles.cronjob_sidecar_mitigation import CronJobSidecarBlocksCompletionMitigationOracle
    from sregym.conductor.oracles.cumulative_admission_webhook_timeout_mitigation import (
        CumulativeAdmissionWebhookTimeoutMitigationOracle,
    )
    from sregym.conductor.oracles.finalizer_deadlock_controller_mitigation import (
        FinalizerDeadlockControllerMitigationOracle,
    )
    from sregym.conductor.oracles.hpa_control_plane_mitigation import HPAControlPlaneMitigationOracle
    from sregym.conductor.oracles.priority_preemption_mitigation import PriorityPreemptionMitigationOracle

    dep = deployment(replicas=total)
    api = Mock()
    api.list_namespaced_deployment.return_value = SimpleNamespace(items=[dep])
    api.read_namespaced_deployment.return_value = dep

    for klass, method in [
        (CalicoRouteReflectorMitigationOracle, "_deployments_unready"),
        (PriorityPreemptionMitigationOracle, "_any_deployment_unready"),
        (CronJobSidecarBlocksCompletionMitigationOracle, "_unhealthy_deployment"),
    ]:
        oracle = object.__new__(klass)
        oracle.apps_v1 = api
        assert (getattr(oracle, method)("example") is None) is expected

    controller = object.__new__(FinalizerDeadlockControllerMitigationOracle)
    controller.controller_deployment_name = "frontend"
    assert controller._check_controller_healthy(SimpleNamespace(apps_v1_api=api), "example")[0] is expected

    webhook = object.__new__(CumulativeAdmissionWebhookTimeoutMitigationOracle)
    webhook.problem = SimpleNamespace(TARGET_DEPLOYMENT="frontend", namespace="example")
    webhook.apps_v1 = api
    webhook.core_v1 = Mock()
    webhook.core_v1.read_namespaced_endpoints.return_value = SimpleNamespace(
        subsets=[SimpleNamespace(addresses=["pod"])]
    )
    assert webhook._pod_healthy()[0] is expected

    hpa = HPAControlPlaneMitigationOracle(
        SimpleNamespace(namespace="example"), deployment_name="frontend", hpa_name="frontend"
    )
    hpa._kubectl_json = Mock(
        return_value={"items": [{"metadata": {"name": "frontend"}, "status": {"phase": "Running"}}]}
    )
    assert hpa._deployment_ready(client.ApiClient().sanitize_for_serialization(dep))[0] is expected
