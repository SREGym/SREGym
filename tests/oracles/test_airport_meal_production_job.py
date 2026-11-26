"""Exercise the production verdict through the real Flink response parser."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.oracles.airport_meal_clearing_mitigation import AirportMealClearingOracle
from sregym.conductor.problems.airport_meal_stream import Stream

JOB = "1" * 32
OTHER_JOB = "2" * 32
RELEASE = {
    "job_id": JOB,
    "jobmanager": "deployment/flink-118-jobmanager",
    "route": "production",
    "target_topic": "issue-orders",
    "reader": "allowances-control",
    "status": "RUNNING",
    "parallelism": 2,
}
JOB_DETAILS = {
    "jid": JOB,
    "state": "RUNNING",
    "vertices": [{"name": "Source: allowances-control -> Writer", "parallelism": 2}],
}
MISSING = f"org.apache.flink.runtime.rest.NotFoundException: Job {JOB} not found\n\tat job handler"


def production_oracle(response):
    stream = Stream()
    stream.release = Mock(return_value=RELEASE)
    stream.job = Mock(return_value=JOB_DETAILS)
    stream.artifacts = Mock(return_value=[f"job-{JOB}"])
    stream.read_artifact = Mock(return_value={"job": JOB_DETAILS})
    stream._kube = Mock(side_effect=[response])
    oracle = AirportMealClearingOracle(SimpleNamespace(stream=stream, airport_meal_expected={}))
    oracle.departure_outcomes = Mock(return_value={})
    return stream, oracle


@pytest.mark.parametrize(
    "response,reason,failure_class",
    [
        (json.dumps({"errors": [MISSING]}) + "\n404", "live_issue_mismatch", "agent_error"),
        ("<404>\n404", "airport_meal_observation_unavailable", "ambiguous"),
        (
            json.dumps({"errors": [MISSING.replace(JOB, OTHER_JOB)]}) + "\n404",
            "airport_meal_observation_unavailable",
            "ambiguous",
        ),
        ('{"errors":["JobManager unavailable"]}\n503', "airport_meal_observation_unavailable", "ambiguous"),
        (RuntimeError("kubectl exec: JobManager pod unavailable"), "airport_meal_observation_unavailable", "ambiguous"),
        ("{\n200", "airport_meal_observation_unavailable", "ambiguous"),
        (json.dumps(JOB_DETAILS | {"jid": OTHER_JOB}) + "\n200", "airport_meal_observation_unavailable", "ambiguous"),
        (
            json.dumps(JOB_DETAILS | {"vertices": [{"name": "Source: allowances-control"}]}) + "\n200",
            "airport_meal_observation_unavailable",
            "ambiguous",
        ),
    ],
)
def test_live_production_lookup_classifies_flink_replies(response, reason, failure_class):
    stream, oracle = production_oracle(response)
    verdict = oracle.evaluate()
    assert (verdict["reason"], verdict["failure_class"]) == (reason, failure_class)
    assert verdict["detail"]["stage"] == "precheck"
    if reason == "live_issue_mismatch":
        assert verdict["detail"]["view"] == "missing live production job"
        assert verdict["detail"]["job_id"] == JOB
    assert stream._kube.call_args.args[2] == RELEASE["jobmanager"]
    assert stream._kube.call_args.args[-1] == f"http://127.0.0.1:8081/jobs/{JOB}"
    stream.job.assert_not_called()
    stream.artifacts.assert_not_called()
    stream.read_artifact.assert_not_called()


def test_live_production_lookup_accepts_confirmed_running_job():
    stream, oracle = production_oracle(json.dumps(JOB_DETAILS) + "\n200")
    assert oracle._production_release(JOB) == RELEASE
    stream._kube.assert_called_once()
