import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.oracles.data_plane_progress import DataPlaneProgressOracle, PipelineValidationError
from sregym.generators.fault.inject_kafka import KafkaFaultInjector


class _Broker:
    def __init__(self, source, output, group_offset=24):
        self.records = {
            "orders-fulfillment": source,
            "orders-processed": output,
        }
        self.committed = group_offset

    def topic_end_offset(self, topic):
        return len(self.records[topic])

    def read_topic(self, topic, end_offset):
        return self.records[topic][:end_offset]

    def group_offset(self, group, topic):
        return self.committed

    def pipeline_snapshot(self, source_topic, output_topic, group):
        return {
            "source": [{"offset": offset, "value": value} for offset, value in enumerate(self.records[source_topic])],
            "output": [{"offset": offset, "value": value} for offset, value in enumerate(self.records[output_topic])],
            "group_offset": self.committed,
        }


def _source_records():
    return [
        *KafkaFaultInjector.initial_records(),
        KafkaFaultInjector.INVALID_RECORD,
        json.dumps({"order_id": "ORD-200021", "amount": 2}),
        json.dumps({"order_id": "ORD-200022", "amount": 3}),
    ]


def _output_records(source, omit=None):
    output = []
    for offset, value in enumerate(source):
        if offset == omit:
            continue
        try:
            order_id = json.loads(value)["order_id"]
        except (json.JSONDecodeError, KeyError):
            continue
        output.append(json.dumps({"source_offset": offset, "order_id": order_id}))
    return output


def _oracle(source, output):
    oracle = object.__new__(DataPlaneProgressOracle)
    oracle.problem = SimpleNamespace(poison_offset=20)
    oracle.topic = "orders-fulfillment"
    oracle.output_topic = "orders-processed"
    oracle.consumer_group = "orders-validator"
    oracle.broker = _Broker(source, output)
    return oracle


def test_snapshot_accepts_complete_matching_results_around_invalid_record():
    source = _source_records()
    processed, group_offset = _oracle(source, _output_records(source))._pipeline_snapshot()

    assert set(processed) == set(range(20)) | {21, 22}
    assert group_offset == 24


def test_snapshot_rejects_skipped_valid_record():
    source = _source_records()

    with pytest.raises(PipelineValidationError, match="valid source records were skipped"):
        _oracle(source, _output_records(source, omit=7))._pipeline_snapshot()


def test_snapshot_rejects_fabricated_processed_result():
    source = _source_records()
    output = _output_records(source)
    output[-1] = json.dumps({"source_offset": 22, "order_id": "ORD-NOT-IN-SOURCE"})

    with pytest.raises(PipelineValidationError, match="does not match source"):
        _oracle(source, output)._pipeline_snapshot()


def test_snapshot_rejects_topic_recreation_that_removed_incident_history():
    source = _source_records()
    source[20] = json.dumps({"order_id": "ORD-REPLACED", "amount": 1})

    with pytest.raises(PipelineValidationError, match="original invalid source record is no longer present"):
        _oracle(source, _output_records(source))._pipeline_snapshot()


def _snapshots():
    source = _source_records()
    snapshots = []
    for index in range(3):
        broker = _Broker(source, _output_records(source), group_offset=len(source))
        snapshots.append(broker.pipeline_snapshot("orders-fulfillment", "orders-processed", "orders-validator"))
        source = [*source, json.dumps({"order_id": f"ORD-NEW-{index}"})]
    return snapshots


def _evaluation(monkeypatch, snapshots):
    source = _source_records()
    oracle = _oracle(source, _output_records(source))
    oracle.settle_seconds = 0
    oracle.progress_timeout = 1
    oracle.progress_window_seconds = 0
    oracle.restart_recover_timeout = 1
    oracle.problem.namespace = "app"
    oracle.problem.kubectl = SimpleNamespace(core_v1_api=Mock())
    old = SimpleNamespace(metadata=SimpleNamespace(name="consumer-old", uid="old"))
    new = SimpleNamespace(metadata=SimpleNamespace(name="consumer-new", uid="new"))
    oracle._ready_consumer_pods = Mock(side_effect=[[old], [new], [new], [new]])
    pending = iter(snapshots)

    def snapshot(*args):
        state = next(pending)
        if isinstance(state, Exception):
            raise state
        return state

    oracle.broker.pipeline_snapshot = snapshot
    clock = SimpleNamespace(now=0)

    def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr("sregym.conductor.oracles.data_plane_progress.time.time", lambda: clock.now)
    monkeypatch.setattr("sregym.conductor.oracles.data_plane_progress.time.sleep", sleep)
    return oracle


@pytest.mark.parametrize("stage", ["initial", "window", "restart"])
@pytest.mark.parametrize("failure", ["fabricated_output", "removed_source", "command", "unreadable_response"])
def test_evaluation_distinguishes_data_errors_from_snapshot_errors_at_each_stage(monkeypatch, stage, failure):
    snapshots = _snapshots()
    stage_index = ["initial", "window", "restart"].index(stage)
    broken = copy.deepcopy(snapshots[stage_index])
    if failure == "fabricated_output":
        broken["output"][-1]["value"] = json.dumps({"source_offset": 22, "order_id": "ORD-FABRICATED"})
    elif failure == "removed_source":
        broken["source"][20]["value"] = json.dumps({"order_id": "ORD-REPLACED"})
    elif failure == "command":
        broken = RuntimeError("unable to read broker snapshot")
    else:
        broken = ValueError("unreadable broker response")
    oracle = _evaluation(monkeypatch, [*snapshots[:stage_index], broken])

    result = oracle.evaluate()

    assert result["success"] is False
    invalid_data = failure in ("fabricated_output", "removed_source")
    assert result["reason"] == ("pipeline_data_invalid" if invalid_data else "pipeline_snapshot_failed")
    assert result["failure_class"] == ("agent_error" if invalid_data else "ambiguous")
    assert isinstance(result["detail"]["error"], str)
    json.dumps(result)


@pytest.mark.parametrize("stage", ["initial", "window", "restart"])
def test_readable_stalled_pipeline_keeps_its_original_failure_category(monkeypatch, stage):
    snapshots = _snapshots()
    if stage == "initial":
        snapshots[0]["output"] = snapshots[0]["output"][:20]
        snapshots[0]["group_offset"] = 20
    elif stage == "window":
        snapshots[1] = snapshots[0]
    else:
        snapshots[2] = snapshots[1]
    oracle = _evaluation(monkeypatch, snapshots)

    result = oracle.evaluate()

    assert (
        result["reason"]
        == {
            "initial": "fault_still_present",
            "window": "no_forward_progress",
            "restart": "not_restart_resistant",
        }[stage]
    )
    assert result["failure_class"] == "agent_error"


@pytest.mark.parametrize("stage", ["initial", "restart"])
@pytest.mark.parametrize("recovered", [False, True])
def test_polling_uses_the_latest_snapshot_after_a_transient_error(monkeypatch, stage, recovered):
    snapshots = _snapshots()
    error = RuntimeError("temporary inspection failure")
    if stage == "initial":
        if not recovered:
            snapshots[0]["output"] = snapshots[0]["output"][:20]
            snapshots[0]["group_offset"] = 20
        snapshots.insert(0, error)
    else:
        if not recovered:
            snapshots[2] = snapshots[1]
        snapshots.insert(2, error)
    oracle = _evaluation(monkeypatch, snapshots)
    oracle.progress_timeout = oracle.restart_recover_timeout = 11

    result = oracle.evaluate()

    assert result["success"] is recovered
    if not recovered:
        assert result["reason"] == ("fault_still_present" if stage == "initial" else "not_restart_resistant")
        assert result["failure_class"] == "agent_error"


def test_evaluation_accepts_valid_progress_that_survives_restart(monkeypatch):
    oracle = _evaluation(monkeypatch, _snapshots())

    assert oracle.evaluate() == {"success": True}
    oracle.problem.kubectl.core_v1_api.delete_namespaced_pod.assert_called_once()
