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
    oracle = AlertOracle("social-network", buffer_seconds=0, sustained_silence_seconds=0, resolve_grace_seconds=0)
    assert oracle.validate().success is False

    response.stdout = json.dumps({"status": "success", "data": {"alerts": []}})
    assert oracle.validate().success is True


class _FakeClock:
    """Deterministic clock: ``sleep`` advances ``monotonic`` without waiting."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _alert_oracle_with_script(monkeypatch, firing_per_poll: list[bool]):
    """Build an AlertOracle whose polls follow ``firing_per_poll`` (last value repeats)."""
    from clients.stratus.weak_oracles import alert_oracle

    clock = _FakeClock()
    polls: list[float] = []
    alert = {"state": "firing", "labels": {"namespace": "astronomy-shop", "alertname": "HighRequestErrorRate"}}

    def fake_query(self):
        index = min(len(polls), len(firing_per_poll) - 1)
        polls.append(clock.now)
        return [alert] if firing_per_poll[index] else []

    monkeypatch.setattr(alert_oracle.AlertOracle, "_query_firing_alerts", fake_query)
    monkeypatch.setattr(alert_oracle, "_get_benchmark_status", lambda: "mitigation")
    monkeypatch.setattr(alert_oracle.time, "sleep", clock.sleep)
    monkeypatch.setattr(alert_oracle.time, "monotonic", clock.monotonic)
    oracle = AlertOracle(
        "astronomy-shop",
        buffer_seconds=30,
        poll_interval_seconds=10,
        sustained_silence_seconds=120,
        resolve_grace_seconds=180,
    )
    return oracle, clock, polls


def test_alerts_that_clear_within_grace_pass(monkeypatch):
    # Prometheus needs ~scrape + evaluation interval to resolve an alert after a
    # fix; the first polls still see it firing.
    oracle, clock, polls = _alert_oracle_with_script(monkeypatch, [True] * 9 + [False])

    result = oracle.validate()

    assert result.success is True
    firing_polls = polls[:9]
    assert firing_polls[-1] - firing_polls[0] == 80  # 9 polls, 10 s apart, all tolerated
    # The silence window starts at the last firing poll and lasts the full 120 s.
    assert clock.now - polls[8] >= 120


def test_alerts_that_persist_fail_after_grace(monkeypatch):
    oracle, clock, polls = _alert_oracle_with_script(monkeypatch, [True])

    result = oracle.validate()

    assert result.success is False
    assert result.issues == ["Firing alerts: HighRequestErrorRate"]
    assert polls[-1] - polls[0] >= 180
    assert polls[-1] - polls[0] < 180 + 10


def test_refiring_alert_restarts_silence_window(monkeypatch):
    # Silent for 50 s, one more firing poll, then silent: the pass must wait for a
    # full 120 s of silence after the re-fire, not 120 s since the first poll.
    script = [False] * 5 + [True] + [False]
    oracle, clock, polls = _alert_oracle_with_script(monkeypatch, script)

    result = oracle.validate()

    assert result.success is True
    refire_time = polls[5]
    assert clock.now - refire_time >= 120


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
