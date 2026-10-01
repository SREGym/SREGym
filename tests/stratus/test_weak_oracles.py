"""Stratus weak-oracle transport and verdict tests."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from clients.stratus.stratus_agent.driver import driver
from clients.stratus.weak_oracles.alert_oracle import AlertOracle
from clients.stratus.weak_oracles.base_oracle import OracleResult
from clients.stratus.weak_oracles.cluster_state_oracle import ClusterStateOracle


def test_cluster_check_uses_proxy_and_problem_namespace(monkeypatch):
    from kubernetes import client, config

    from clients.stratus.weak_oracles import cluster_state_oracle

    seen = {}
    monkeypatch.setenv("HTTPS_PROXY", "http://egress-proxy:3128")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setattr(cluster_state_oracle.os.path, "exists", lambda _: True)
    monkeypatch.setattr(config, "load_kube_config", lambda: None)

    def fake_api_client(configuration):
        seen["proxy"] = configuration.proxy
        seen["no_proxy"] = configuration.no_proxy
        return object()

    monkeypatch.setattr(client, "ApiClient", fake_api_client)
    # Return an empty pod list and record the requested namespace.
    monkeypatch.setattr(
        client,
        "CoreV1Api",
        lambda _: SimpleNamespace(
            list_namespaced_pod=lambda namespace: seen.update(namespace=namespace) or SimpleNamespace(items=[])
        ),
    )

    result = ClusterStateOracle("social-network").validate()
    assert result.success is True
    assert seen == {
        "proxy": "http://egress-proxy:3128",
        "no_proxy": "localhost,127.0.0.1",
        "namespace": "social-network",
    }


def test_cluster_connection_error_is_inconclusive(monkeypatch):
    from kubernetes import config

    monkeypatch.setattr(config, "load_kube_config", lambda: (_ for _ in ()).throw(ConnectionError("unreachable")))
    monkeypatch.setattr(config, "load_incluster_config", lambda: (_ for _ in ()).throw(ConnectionError("unreachable")))
    result = ClusterStateOracle("social-network").validate()
    assert result.success is None
    assert "unreachable" in result.issues[0]


@pytest.mark.parametrize("failure", ["forbidden", "invalid-json", "timeout"])
def test_alert_query_failure_is_inconclusive(monkeypatch, failure):
    from clients.stratus.weak_oracles import alert_oracle

    def fake_run(cmd, **kwargs):
        assert cmd[:5] == ["kubectl", "exec", "-n", "observe", "deploy/prometheus-server"]
        assert "http://localhost:9090/api/v1/alerts" in cmd
        if failure == "timeout":
            raise alert_oracle.subprocess.TimeoutExpired(cmd, 15)
        if failure == "invalid-json":
            return SimpleNamespace(returncode=0, stdout="not json", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="Forbidden")

    monkeypatch.setattr(alert_oracle.subprocess, "run", fake_run)
    monkeypatch.setattr(alert_oracle, "_get_benchmark_status", lambda: "mitigation")
    monkeypatch.setattr(alert_oracle.time, "sleep", lambda _: None)
    result = AlertOracle("social-network", buffer_seconds=0, sustained_silence_seconds=0).validate()
    assert result.success is None


def test_alert_query_distinguishes_firing_from_silence(monkeypatch):
    from clients.stratus.weak_oracles import alert_oracle

    alerts = [{"state": "firing", "labels": {"namespace": "social-network", "alertname": "Broken"}}]
    response = SimpleNamespace(
        returncode=0, stdout=json.dumps({"status": "success", "data": {"alerts": alerts}}), stderr=""
    )
    monkeypatch.setattr(alert_oracle.subprocess, "run", lambda *args, **kwargs: response)
    monkeypatch.setattr(alert_oracle, "_get_benchmark_status", lambda: "mitigation")
    monkeypatch.setattr(alert_oracle.time, "sleep", lambda _: None)
    oracle = AlertOracle("social-network", buffer_seconds=0, sustained_silence_seconds=0)
    assert oracle.validate().success is False

    response.stdout = json.dumps({"status": "success", "data": {"alerts": []}})
    assert oracle.validate().success is True


def test_unavailable_check_does_not_become_a_failed_verdict():
    oracles = [
        SimpleNamespace(validate=lambda: OracleResult(True, [])),
        SimpleNamespace(validate=lambda: OracleResult(None, ["transport unavailable"])),
    ]
    verdict, issues = driver.validate_oracles(oracles)
    assert verdict is None
    assert len(issues) == 1


@pytest.mark.parametrize("outcomes", [(False, None), (None, False)])
def test_known_failure_takes_precedence_over_unavailable_check(outcomes):
    oracles = [
        SimpleNamespace(validate=lambda outcome=outcome: OracleResult(outcome, ["check failed"]))
        for outcome in outcomes
    ]
    verdict, issues = driver.validate_oracles(oracles)
    assert verdict is False
    assert len(issues) == 2


@pytest.mark.parametrize("retry_mode", ["naive", "validate"])
@pytest.mark.parametrize("oracle_error", [False, True])
def test_inconclusive_oracle_submits_without_rollback(monkeypatch, retry_mode, oracle_error):
    from clients.stratus.stratus_agent.driver import driver as module

    config_text = (
        f"max_step: 1\nmax_retry_attempts: 2\nretry_mode: {retry_mode}\nprompts_path: mitigation_agent_prompts.yaml\n"
    )

    # The driver resolves its config relative to its source; isolate that read.
    original_read_text = module.Path.read_text

    def fake_read_text(path, *args, **kwargs):
        if path.name == "mitigation_agent_config.yaml":
            return config_text
        if path.name == "llm_summarization_prompt.yaml":
            return "mitigation_retry_prompt: retry"
        if path.name == "mitigation_agent_prompts.yaml":
            return "system: system\nuser: '{app_name} {app_namespace} {max_step} {faults_info} {app_description}'\nretry_user: '{last_result} {reflection}'"
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(module.Path, "read_text", fake_read_text)
    monkeypatch.setattr(module, "get_app_info", lambda: {"app_name": "App", "descriptions": "desc", "namespace": "app"})

    def fake_validate(_):
        if oracle_error:
            raise ConnectionError("unavailable")
        return None, [OracleResult(None, ["unavailable"])]

    monkeypatch.setattr(module, "validate_oracles", fake_validate)
    calls = []

    async def fake_agent(_):
        calls.append("agent")
        agent = SimpleNamespace(
            callback=SimpleNamespace(
                usage_metadata={"model": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
            )
        )
        state = SimpleNamespace(values={"num_steps": 1, "submitted": True, "executed_commands": []})
        return agent, state, []

    async def fake_submit(*args, **kwargs):
        calls.append("submit")

    async def fake_rollback(*args, **kwargs):
        calls.append("rollback")

    monkeypatch.setattr(module, "mitigation_agent_single_run", fake_agent)
    monkeypatch.setattr(module, "manual_submit_tool", fake_submit)
    monkeypatch.setattr(module, "perform_rollback", fake_rollback)
    asyncio.run(module.mitigation_task_main("diagnosis"))
    assert calls == ["agent", "submit"]
