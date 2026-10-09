"""Causal controller checks; live TCP/GTID qualification is a separate gate."""

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sregym.generators.fault.regional_database_failover import RegionalFailoverFault
from sregym.generators.workload.codehub import ReceiptLedger
from sregym.generators.workload.codehub_seed import RegionEndpoints, TenantAccount


class IncidentModel:
    def __init__(self, ledger, *, pending=4, leak=False):
        self.writer = SimpleNamespace(name="old", region="region-a", role="writer", origin="mysql://old.a.svc:3306")
        self.candidate = SimpleNamespace(
            name="new", region="region-b", role="candidate", origin="mysql://new.b.svc:3306"
        )
        self.database_groups = (SimpleNamespace(name="group-0", members=(self.writer, self.candidate)),)
        self.rows = {"old": set(), "new": set()}
        self.partition, self.promoted, self.restored = False, False, False
        self.ledger, self.pending, self.leak = ledger, pending, leak
        self.calls = []

    def mysql_command(self, member, query):
        self.calls.append(query)
        if "PROCESSLIST" in query:
            return "ID\n42\n"
        if "WHERE event_id=" in query:
            identity = query.split("event_id='")[1].split("'")[0]
            return json.dumps({"present": int(identity in self.rows[member.name])})
        if "outbox" in query:
            return json.dumps({"count": self.pending})
        return ""

    def apply(self):
        self.partition = True

    def restore(self):
        self.partition = False
        self.restored = True

    def client(self, origin, _token, ledger):
        model = self

        class Client:
            def submit(self, operation, *, epoch, effects=(), **_kwargs):
                ledger.request(operation, epoch, effects=effects)
                member = "old" if origin.endswith("a") else "new"
                model.rows[member].add(operation.event_id)
                if member == "old" and (not model.partition or model.leak):
                    model.rows["new"].add(operation.event_id)
                ledger.acknowledge(operation.event_id, origin, 1, 201)
                return True

            def close(self):
                pass

        return Client()

    def topology_client(self, **_kwargs):
        model = self

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                if model.partition:
                    model.promoted = True
                return {"promoted": model.promoted}

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def get(self, _url):
                return Response()

        return Client()


def fault(tmp_path, monkeypatch, *, pending=4, leak=False):
    ledger = ReceiptLedger(tmp_path / "receipts.sqlite")
    model = IncidentModel(ledger, pending=pending, leak=leak)
    account = TenantAccount(
        str(uuid4()), str(uuid4()), "region-a", "group-0", str(uuid4()), "o" * 40, webhook_id=str(uuid4())
    )
    endpoints = {
        "region-" + letter: RegionEndpoints("http://api-" + letter, "http://git-" + letter, "http://topology-" + letter)
        for letter in "ab"
    }
    monkeypatch.setattr("sregym.generators.fault.regional_database_failover.httpx.Client", model.topology_client)
    monkeypatch.setattr("sregym.generators.fault.regional_database_failover.time.sleep", lambda _seconds: None)
    instance = RegionalFailoverFault(
        model,
        endpoints,
        account,
        ledger,
        Path(tmp_path),
        service_token="service" * 8,
        delivery_url="http://receiver/deliveries",
        network=model,
        client_factory=model.client,
    )
    return instance, model, ledger


def test_both_histories_backlog_and_restoration_precede_closed_handoff(tmp_path, monkeypatch):
    instance, model, ledger = fault(tmp_path, monkeypatch)
    evidence = instance.inject(suffix_operations=2)
    assert model.promoted and model.restored and not model.partition
    assert evidence.connectivity_restored_at_ns >= evidence.promoted_at_ns
    assert evidence.old_writer_retains_suffix and evidence.new_writer_retains_suffix
    assert evidence.pending_jobs == 4 and evidence.regional_latency_qualified is False
    assert "KILL CONNECTION 42;" in model.calls
    assert not any("STOP REPLICA" in query for query in model.calls)
    assert ledger.cut().operations == 5
    assert len(ledger.expected_effects({instance.account.tenant_id: "group-0"})["group-0"]) == 10
    ledger.close()


@pytest.mark.parametrize(
    "settings,message", [({"leak": True}, "partition did not halt"), ({"pending": 0}, "actual unprocessed")]
)
def test_false_partition_or_empty_backlog_never_qualifies(tmp_path, monkeypatch, settings, message):
    instance, model, ledger = fault(tmp_path, monkeypatch, **settings)
    with pytest.raises(RuntimeError, match=message):
        instance.inject(suffix_operations=1)
    assert model.restored and not model.partition
    assert instance.evidence is None and ledger.cut().operations == 0
    ledger.close()
