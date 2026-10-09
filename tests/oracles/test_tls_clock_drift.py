"""Clock repair must retain TLS, application state and a fresh handshake."""

import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.oracles.tls_clock_drift_mitigation import TLSClockDriftMitigationOracle
from sregym.conductor.problems import registry
from sregym.conductor.problems.node_clock_drift import NodeClockDriftHotelReservation
from sregym.conductor.problems.tls_clock_drift import (
    CLOCK_CONFIG,
    CLOCK_KEY,
    TLS_CA,
    TLS_SECRET,
    TLS_SERVICE,
    TLSClockDriftHotelReservation,
    tls_client_container,
    tls_client_volumes,
)


@pytest.fixture
def oracle(monkeypatch):
    core, api = Mock(), Mock()
    problem = SimpleNamespace(
        namespace="application",
        core_v1=core,
        task_version="tls-validation-clock-v2",
        kubectl=SimpleNamespace(apps_v1_api=api),
    )
    oracle = TLSClockDriftMitigationOracle(problem)
    oracle.replica_count = {"frontend": 1, TLS_SERVICE: 1}
    oracle.expected_secret = {"tls.crt": "original-cert", "tls.key": "original-key"}
    oracle.expected_ca = {"ca.crt": "original-ca"}
    core.read_namespaced_secret.return_value.data = copy.deepcopy(oracle.expected_secret)
    ca = SimpleNamespace(data=copy.deepcopy(oracle.expected_ca))
    settings = SimpleNamespace(data={CLOCK_KEY: "0"})
    core.read_namespaced_config_map.side_effect = lambda name, _: ca if name == TLS_CA else settings
    frontend = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [tls_client_container()],
                    "volumes": tls_client_volumes(),
                }
            }
        }
    }
    server = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "server",
                            "image": tls_client_container()["image"],
                            "command": [
                                "openssl",
                                "s_server",
                                "-accept",
                                "9443",
                                "-cert",
                                "/etc/tls/tls.crt",
                                "-key",
                                "/etc/tls/tls.key",
                                "-www",
                                "-quiet",
                            ],
                            "volumeMounts": [{"name": "tls", "mountPath": "/etc/tls", "readOnly": True}],
                        }
                    ],
                    "volumes": [{"name": "tls", "secret": {"secretName": TLS_SECRET}}],
                }
            }
        }
    }
    api.read_namespaced_deployment.side_effect = lambda name, _: frontend if name == "frontend" else server
    core.read_namespaced_service.return_value = {
        "spec": {"selector": {"app": TLS_SERVICE}, "ports": [{"port": 9443, "targetPort": 9443}]},
    }
    pod = client.ApiClient()._ApiClient__deserialize(
        {"metadata": {"name": "frontend-pod"}, "spec": frontend["spec"]["template"]["spec"]},
        "V1Pod",
    )
    core.list_namespaced_pod.return_value.items = [pod]
    monkeypatch.setattr(MitigationOracle, "evaluate", Mock(return_value={"success": True}))
    oracle._fresh_tls_handshake = Mock(return_value=True)
    return oracle, settings, frontend, pod


def test_clock_repair_requires_a_fresh_real_handshake(oracle):
    instance, _, _, _ = oracle
    assert instance.evaluate()["success"] is True
    instance._fresh_tls_handshake.assert_called_once_with("original-ca", "0")
    instance._fresh_tls_handshake.return_value = False
    assert instance.evaluate()["reason"] == "tls_handshake_failed"


@pytest.mark.parametrize("offset", ["2592000", "-2592000", "garbage", "0; true", "", "1+2", "999999999999999"])
def test_noop_or_malformed_clock_cannot_pass(oracle, offset):
    instance, settings, _, _ = oracle
    settings.data[CLOCK_KEY] = offset
    assert instance.evaluate()["success"] is False
    instance._fresh_tls_handshake.assert_not_called()


@pytest.mark.parametrize(
    "change", ["remove_client", "skip_readiness", "disable_hostname", "override_environment", "redirect_volume"]
)
def test_disabling_or_replacing_tls_validation_cannot_pass(oracle, change):
    instance, _, frontend, _ = oracle
    spec = frontend["spec"]["template"]["spec"]
    container = spec["containers"][0]
    if change == "remove_client":
        spec["containers"] = []
    elif change == "skip_readiness":
        container["readinessProbe"]["exec"]["command"] = ["true"]
    elif change == "disable_hostname":
        container["args"] = ["while true; do sleep 30; done"]
    elif change == "override_environment":
        container["env"] = [{"name": "LD_PRELOAD", "value": "/tmp/bypass.so"}]
    else:
        spec["volumes"][0] = {"name": "tls-ca", "emptyDir": {}}
    assert instance.evaluate()["reason"] == "tls_validation_contract_changed"
    instance._fresh_tls_handshake.assert_not_called()


def test_altered_live_pod_cannot_hide_behind_a_correct_deployment_template(oracle):
    instance, _, _, pod = oracle
    pod.spec.containers[0].args = ["true"]
    assert instance.evaluate()["reason"] == "tls_validation_contract_changed"


def test_kubernetes_automatic_service_account_mount_is_not_a_tls_contract_change(oracle):
    instance, _, _, pod = oracle
    pod.spec.volumes.append(
        client.V1Volume(
            name="kube-api-access-abcde",
            projected=client.V1ProjectedVolumeSource(
                sources=[
                    client.V1VolumeProjection(
                        service_account_token=client.V1ServiceAccountTokenProjection(path="token")
                    ),
                ]
            ),
        )
    )
    pod.spec.containers[0].volume_mounts.append(
        client.V1VolumeMount(
            name="kube-api-access-abcde",
            mount_path="/var/run/secrets/kubernetes.io/serviceaccount",
            read_only=True,
        )
    )
    assert instance.evaluate()["success"] is True


@pytest.mark.parametrize("resource", ["secret", "ca"])
def test_replacing_the_certificate_or_trust_bundle_cannot_pass(oracle, resource):
    instance, _, _, _ = oracle
    if resource == "secret":
        instance.problem.core_v1.read_namespaced_secret.return_value.data["tls.crt"] = "replacement"
    else:
        instance.problem.core_v1.read_namespaced_config_map(TLS_CA, "application").data["ca.crt"] = "replacement"
    assert instance.evaluate()["reason"] == "tls_identity_or_trust_changed"


def test_missing_expected_baseline_never_recaptures_agent_state(oracle):
    instance, _, _, _ = oracle
    instance.expected_ca = None
    result = instance.evaluate()
    assert result["reason"] == "oracle_raised"
    assert result["failure_class"] == "harness_error"
    assert instance.problem.core_v1.mock_calls == []


def test_missing_tls_dependency_fails_closed(oracle):
    instance, _, _, _ = oracle
    instance.problem.core_v1.read_namespaced_secret.side_effect = ApiException(status=404)
    assert instance.evaluate()["reason"] == "required_tls_resource_missing"


def test_application_deletion_or_scaling_failure_is_preserved(oracle, monkeypatch):
    instance, _, _, _ = oracle
    monkeypatch.setattr(
        MitigationOracle,
        "evaluate",
        Mock(return_value={"success": False, "reason": "required_deployment_scaled_to_zero"}),
    )
    assert instance.evaluate()["reason"] == "required_deployment_scaled_to_zero"
    instance._fresh_tls_handshake.assert_not_called()


@pytest.mark.parametrize("emulated", [True, False])
def test_existing_clock_id_resolves_to_an_explicit_platform_version(monkeypatch, emulated):
    monkeypatch.setattr(registry, "rootless_workload_enabled", lambda: False)
    holder = object.__new__(registry.ProblemRegistry)
    holder.PROBLEM_REGISTRY = {"node_clock_drift_hotel_reservation": Mock(return_value="native-v1")}
    holder.non_emulated_cluster_problems = ["node_clock_drift_hotel_reservation"]
    holder.kubectl = Mock()
    holder.kubectl.is_emulated_cluster.return_value = emulated
    portable = Mock(return_value="tls-v2")
    monkeypatch.setattr(registry, "TLSClockDriftHotelReservation", portable)
    assert holder.get_problem_instance("node_clock_drift_hotel_reservation") == ("tls-v2" if emulated else "native-v1")


def test_direct_native_clock_injection_refuses_kind_before_any_mutation():
    problem = object.__new__(NodeClockDriftHotelReservation)
    problem.kubectl = Mock()
    problem.kubectl.is_emulated_cluster.return_value = True
    problem._setup_tls_infrastructure = Mock()
    with pytest.raises(RuntimeError, match="Native node clock drift"):
        problem.inject_fault()
    problem._setup_tls_infrastructure.assert_not_called()


def test_clock_recovery_restores_configuration_without_hiding_api_errors():
    problem = object.__new__(TLSClockDriftHotelReservation)
    problem.namespace = "application"
    problem.core_v1 = Mock()
    problem.recover_fault()
    problem.core_v1.patch_namespaced_config_map.assert_called_once_with(
        CLOCK_CONFIG, "application", {"data": {CLOCK_KEY: "0"}}
    )
    problem.core_v1.patch_namespaced_config_map.side_effect = ApiException(status=403)
    with pytest.raises(ApiException):
        problem.recover_fault()
