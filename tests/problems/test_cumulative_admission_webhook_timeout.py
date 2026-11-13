from unittest.mock import Mock

import pytest
from kubernetes.client.exceptions import ApiException

from sregym.conductor.problems import cumulative_admission_webhook_timeout_hotel_reservation as module
from sregym.conductor.problems.apps.slack_spine.admission import CumulativeAdmissionWebhookTimeoutSlack


@pytest.fixture(params=["hotel", "slack"])
def problem(request):
    cls = (
        module.CumulativeAdmissionWebhookTimeoutHotelReservation
        if request.param == "hotel"
        else CumulativeAdmissionWebhookTimeoutSlack
    )
    problem = cls.__new__(cls)
    problem.namespace = "hotel-reservation" if request.param == "hotel" else "slack-spine"
    problem.core_v1 = Mock()
    problem.apps_v1 = Mock()
    return problem


def admission_timeout():
    exc = ApiException(status=504)
    exc.body = '{"reason":"Timeout","message":"request did not complete - context deadline exceeded"}'
    return exc


def test_backend_readiness_requires_the_webhook_listener(problem):
    problem._ensure_backend_deployment("audit-log-enforcer")
    body = problem.apps_v1.create_namespaced_deployment.call_args.kwargs["body"]
    probe = body.spec.template.spec.containers[0].readiness_probe
    assert probe.tcp_socket.port == problem.WEBHOOK_BACKEND_PORT


def test_admission_check_uses_dry_run_and_waits_for_the_real_deadline(problem):
    problem.core_v1.create_namespaced_pod.side_effect = admission_timeout()
    problem._wait_for_admission_timeout()
    call = problem.core_v1.create_namespaced_pod.call_args.kwargs
    assert call["dry_run"] == "All"
    assert call["namespace"] == problem.namespace
    assert call["body"].metadata.generate_name == "admission-readiness-check-"
    problem.core_v1.delete_namespaced_pod.assert_not_called()


def test_admitted_warmup_probe_is_not_mistaken_for_an_active_fault(problem, monkeypatch):
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    problem.core_v1.create_namespaced_pod.side_effect = [None, admission_timeout()]
    problem._wait_for_admission_timeout()
    assert problem.core_v1.create_namespaced_pod.call_count == 2


@pytest.mark.parametrize("status", [403, 500, 503])
def test_unrelated_api_errors_do_not_confirm_the_fault(problem, status):
    problem.core_v1.create_namespaced_pod.side_effect = ApiException(status=status)
    with pytest.raises(ApiException):
        problem._wait_for_admission_timeout()


def test_other_gateway_timeout_is_not_an_admission_deadline(problem):
    problem.core_v1.create_namespaced_pod.side_effect = ApiException(status=504)
    with pytest.raises(ApiException):
        problem._wait_for_admission_timeout()


def test_available_admission_times_out_without_disrupting_the_application(problem, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(module.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    problem.ADMISSION_PROPAGATION_TIMEOUT_S = 5
    with pytest.raises(RuntimeError, match="refusing to disrupt"):
        problem._wait_for_admission_timeout()


@pytest.mark.parametrize("active", [True, False])
def test_injection_checks_admission_before_deleting_the_target(problem, active):
    steps = []
    for name in (
        "_ensure_namespace",
        "_ensure_tls_secret",
        "_ensure_server_script_configmap",
        "_ensure_backend_deployment",
        "_ensure_backend_service",
        "_wait_for_deployment_ready",
        "_ensure_default_deny_network_policy",
        "_ensure_backend_allow_policies",
        "_remove_apiserver_allow_network_policy",
        "_ensure_mutating_webhook",
        "_install_decoy_webhooks",
        "_wait_for_symptom",
    ):
        setattr(problem, name, Mock())
    problem._generate_tls_material = Mock(return_value=("ca", "cert", "key"))

    def check():
        steps.append("check")
        if not active:
            raise RuntimeError("admission is not blocked")

    problem._wait_for_admission_timeout = check
    problem._delete_target_pod = lambda: steps.append("delete")
    if active:
        problem.inject_fault()
        assert steps == ["check", "delete"]
    else:
        with pytest.raises(RuntimeError, match="not blocked"):
            problem.inject_fault()
        assert steps == ["check"]
