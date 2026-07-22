import json
from types import SimpleNamespace

import pytest

from sregym.conductor.oracles.data_plane_progress import DataPlaneProgressOracle
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

    with pytest.raises(ValueError, match="valid source records were skipped"):
        _oracle(source, _output_records(source, omit=7))._pipeline_snapshot()


def test_snapshot_rejects_fabricated_processed_result():
    source = _source_records()
    output = _output_records(source)
    output[-1] = json.dumps({"source_offset": 22, "order_id": "ORD-NOT-IN-SOURCE"})

    with pytest.raises(ValueError, match="does not match source"):
        _oracle(source, output)._pipeline_snapshot()


def test_snapshot_rejects_topic_recreation_that_removed_incident_history():
    source = _source_records()
    source[20] = json.dumps({"order_id": "ORD-REPLACED", "amount": 1})

    with pytest.raises(ValueError, match="original invalid source record is no longer present"):
        _oracle(source, _output_records(source))._pipeline_snapshot()
