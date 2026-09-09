import shlex
import subprocess
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.feature_flag_http_probe_mitigation import FeatureFlagHttpProbeMitigationOracle
from sregym.service.kubectl import KubeCtl


def _oracle(monkeypatch, responses):
    kube = SimpleNamespace(
        list_pods=lambda namespace: SimpleNamespace(
            items=[SimpleNamespace(metadata=SimpleNamespace(name="consul-1"), status=SimpleNamespace(phase="Running"))]
        ),
        exec_command_checked=Mock(side_effect=responses),
    )
    monkeypatch.setattr("sregym.conductor.oracles.feature_flag_http_probe_mitigation.time.sleep", lambda _: None)
    return FeatureFlagHttpProbeMitigationOracle(SimpleNamespace(namespace="app", kubectl=kube))


@pytest.mark.parametrize("success_count", [0, 3, 4, 5])
def test_http_response_threshold_is_unchanged(monkeypatch, success_count):
    responses = ["HTTP/1.1 200 OK\n"] * success_count + ["HTTP/1.1 500 Internal Server Error\n"] * (5 - success_count)
    oracle = _oracle(monkeypatch, responses)
    result = oracle.evaluate()
    assert result["success"] is (success_count >= 4)
    if not result["success"]:
        assert result["reason"] == "endpoint_error_rate_high"
        assert result["failure_class"] == "agent_error"
    assert oracle.problem.kubectl.exec_command_checked.call_count == 5


@pytest.mark.parametrize("output", ["", "connection refused", "wget: not found", "not HTTP/1.1 200", "HTTP/1.1 2000"])
def test_missing_http_response_is_not_an_http_error(monkeypatch, output):
    oracle = _oracle(monkeypatch, ["HTTP/1.1 200 OK\n"] * 4 + [output])
    result = oracle.evaluate()
    assert result["success"] is False
    assert result["reason"] == "http_probe_response_missing"
    assert result["failure_class"] == "ambiguous"


def test_last_http_status_is_used_after_a_redirect(monkeypatch):
    result = _oracle(monkeypatch, ["HTTP/1.1 302 Found\n  HTTP/1.1 200 OK\n"] * 5).evaluate()
    assert result["success"] is True


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, "kubectl", stderr=b"connection refused"),
        subprocess.TimeoutExpired("kubectl", 30),
    ],
)
def test_real_checked_wrapper_preserves_command_failure(monkeypatch, error):
    oracle = _oracle(monkeypatch, [])
    kube = oracle.problem.kubectl
    kube.exec_command_checked = MethodType(KubeCtl.exec_command_checked, kube)
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=error))
    result = oracle.evaluate()
    assert result["success"] is False
    assert result["reason"] == "oracle_command_failed"
    assert result["failure_class"] == "ambiguous"


@pytest.mark.parametrize("status", [403, 404, 503])
def test_api_error_is_not_counted_as_an_http_response(monkeypatch, status):
    result = _oracle(monkeypatch, [ApiException(status=status)]).evaluate()
    assert result["success"] is False
    assert result["failure_class"] == ("environment_error" if status == 503 else "ambiguous")


def test_only_the_in_pod_wget_exit_status_is_suppressed(monkeypatch):
    oracle = _oracle(monkeypatch, ["HTTP/1.1 500 Internal Server Error\n"] * 5)
    result = oracle.evaluate()
    command = oracle.problem.kubectl.exec_command_checked.call_args.args[0]
    args = shlex.split(command)
    assert args[:8] == ["kubectl", "exec", "consul-1", "-n", "app", "--", "sh", "-c"]
    assert len(args) == 9
    script = args[-1]
    assert "-T 10" in script
    assert oracle.problem.kubectl.exec_command_checked.call_args.kwargs == {"timeout": 30}
    # An HTTP 500 makes wget exit nonzero, but the remote shell preserves its headers.
    stub = "wget() { printf '  HTTP/1.1 500 Internal Server Error\\n' >&2; return 1; }; "
    response = subprocess.run(["sh", "-c", stub + script], check=True, capture_output=True, text=True)
    assert "HTTP/1.1 500" in response.stdout
    assert result["reason"] == "endpoint_error_rate_high"
