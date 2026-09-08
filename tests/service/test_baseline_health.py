import json
from unittest.mock import Mock

import pytest

from scripts.baseline.run import traffic_errors
from sregym.observer.baseline import capture_attempt, parse_cgroups, pod_findings


def pod(*, init=False, oom=True):
    return {
        "metadata": {"namespace": "observe", "name": "loki-0", "uid": "one"},
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
            "initContainerStatuses" if init else "containerStatuses": [
                {
                    "name": "loki",
                    "ready": True,
                    "restartCount": 1,
                    "lastState": {
                        "terminated": {"reason": "OOMKilled" if oom else "Error", "finishedAt": "2026-09-08T01:00:00Z"}
                    },
                }
            ],
        },
    }


@pytest.mark.parametrize("init", [False, True])
def test_recovered_oom_in_monitoring_is_not_healthy(init):
    findings = pod_findings({"items": [pod(init=init)]})
    assert any(f["reason"] == "OOMKilled" and f["namespace"] == "observe" for f in findings)


def test_empty_pod_response_is_not_healthy():
    with pytest.raises(ValueError, match="No pods"):
        pod_findings({"items": []})


def test_cgroup_parser_preserves_events_and_handles_missing_peak():
    parsed = parse_cgroups(
        "CGROUP /sys/fs/cgroup/cri-containerd-abc.scope\n"
        "FILE memory.current\n123\nFILE memory.events\noom 1\noom_kill 1\n"
    )
    assert parsed["abc"]["memory.current"].strip() == "123"
    assert "oom_kill 1" in parsed["abc"]["memory.events"]
    assert "memory.peak" not in parsed["abc"]


def test_healthy_http_requires_running_workload_and_successful_requests():
    body = {
        "products": {"status": 200, "product_count": 5},
        "workload": {"state": "running", "total_rps": 10, "fail_ratio": 0},
    }
    sample = {"http_probe": {"rc": 0, "out": json.dumps(body)}}
    assert traffic_errors(sample, 0.01) == []
    body["workload"]["total_rps"] = 0
    sample["http_probe"]["out"] = json.dumps(body)
    assert "traffic_not_running" in traffic_errors(sample, 0.01)
    assert traffic_errors({}, 0.01)


def test_phase_evidence_is_written_before_returning_oom(tmp_path, monkeypatch):
    data = {"pods": {"rc": 0}, "findings": pod_findings({"items": [pod()]})}
    monkeypatch.setattr("sregym.observer.baseline.collect", Mock(return_value=data))
    assert capture_attempt(tmp_path, "pre-injection", context="test")
    saved = json.loads((tmp_path / "pre-injection.json").read_text())
    assert saved["phase"] == "pre-injection"
    assert saved["findings"]


def test_failed_collection_rejects_baseline(tmp_path, monkeypatch):
    monkeypatch.setattr("sregym.observer.baseline.collect", Mock(return_value={"pods": {"rc": 1}}))
    assert capture_attempt(tmp_path, "pre-injection", context="test") == [{"reason": "pod_collection_failed"}]


def test_unhealthy_baseline_prevents_injection_and_records_incomplete(monkeypatch, tmp_path):
    from sregym.conductor.conductor import Conductor

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SREGYM_BASELINE_CHECK", "1")
    monkeypatch.setattr("sregym.conductor.conductor.capture_attempt", Mock(return_value=[{"reason": "OOMKilled"}]))
    conductor = Conductor.__new__(Conductor)
    conductor.results = {}
    conductor.problem = Mock()
    conductor.record_incomplete_attempt = Mock()
    with pytest.raises(RuntimeError, match="Unhealthy pre-injection"):
        conductor._inject_fault()
    conductor.problem.inject_fault.assert_not_called()
    conductor.record_incomplete_attempt.assert_called_once_with("baseline_unhealthy")


def test_healthy_baseline_allows_intended_fault(monkeypatch, tmp_path):
    from sregym.conductor.conductor import Conductor

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SREGYM_BASELINE_CHECK", "1")
    monkeypatch.setattr("sregym.conductor.conductor.capture_attempt", Mock(side_effect=[[], [{"reason": "OOMKilled"}]]))
    conductor = Conductor.__new__(Conductor)
    conductor.results = {}
    conductor.problem = Mock(mitigation_oracle=None, diagnosis_oracle=None)
    conductor.logger = Mock()
    conductor._inject_fault()
    conductor.problem.inject_fault.assert_called_once()
    assert conductor.fault_injected is True


def test_normal_pod_deletion_does_not_mask_oom_or_fail_readiness():
    retiring = pod()
    retiring["metadata"]["deletionTimestamp"] = "2026-09-08T02:00:00Z"
    retiring["status"]["conditions"][0]["status"] = "False"
    findings = pod_findings({"items": [retiring]})
    assert "pod_not_ready" not in {f["reason"] for f in findings}
    assert "OOMKilled" in {f["reason"] for f in findings}
