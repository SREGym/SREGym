"""Narrow private receipt IO for fresh checks performed inside the verifier.

The owner persists provenance, not verdicts. This object is transported only as
a resource handle over the existing trusted container pipe.
"""

import hashlib
import json
from urllib.parse import urlsplit
from uuid import UUID

from sregym.generators.workload.codehub import ReceiptLedger, canonical, operation_from_row


class CodeHubVerificationJournal:
    OPERATIONS = {
        "journal_begin_epoch": ("begin_epoch", 0),
        "journal_request": ("request", 5),
        "journal_acknowledge": ("acknowledge", 6),
        "journal_reject": ("reject", 5),
        "journal_close_epoch": ("close_epoch", 1),
        "journal_delivery_receipts": ("delivery_receipts", 1),
        "journal_traffic_progress": ("traffic_progress", 0),
        "journal_project_receipts": ("project_receipts", 1),
    }

    def __init__(self, ledger: ReceiptLedger, tenant_groups: dict[str, str], *, observer=None, traffic_source=None):
        with ledger.read_snapshot() as snapshot:
            snapshot.validate_routing(tenant_groups)
        self.ledger = ledger
        self.tenant_groups = dict(tenant_groups)
        self._epochs = set()
        self.observer = observer
        self.traffic_source = traffic_source

    def project_receipts(self, identities):
        if type(identities) not in {list, tuple} or not 1 <= len(identities) <= 16:
            raise ValueError("Parent receipt observation needs a bounded tenant/project batch")
        normalized = []
        for pair in identities:
            if type(pair) not in {list, tuple} or len(pair) != 2:
                raise ValueError("Parent receipt identity must be a tenant/project pair")
            for value in pair:
                if type(value) is not str or str(UUID(value)) != value:
                    raise ValueError("Parent receipt identity must use canonical UUIDs")
            normalized.append(tuple(pair))
        if len(set(normalized)) != len(normalized):
            raise ValueError("Parent receipt identities cannot repeat")
        result = []
        with self.ledger._lock:
            for tenant, project in normalized:
                if tenant not in self.tenant_groups:
                    raise ValueError("Parent receipt observation differs from frozen routing")
                rows = self.ledger._db.execute(
                    "SELECT r.body,x.effects,r.epoch FROM requests r JOIN expectations x USING(event_id) "
                    "JOIN epochs e ON e.id=r.epoch WHERE r.record_key=? AND r.status='acknowledged' "
                    "AND e.closed=1 ORDER BY r.revision LIMIT 33",
                    (f"{tenant}/{project}",),
                ).fetchall()
                if not rows or len(rows) > 32:
                    raise ValueError("Parent history is absent or exceeds its bounded observation")
                operations, effects, epochs = [], [], set()
                for body, expected, epoch in rows:
                    operation = operation_from_row(json.loads(body))
                    if (
                        operation.tenant_id != tenant
                        or operation.entity_id != project
                        or not operation.kind.startswith("project.")
                    ):
                        raise ValueError("Parent receipt does not describe the requested project")
                    operations.append(operation.observed_row())
                    effects.extend(json.loads(expected))
                    epochs.add(epoch)
                result.append(
                    {
                        "group": self.tenant_groups[tenant],
                        "operations": operations,
                        "effects": effects,
                        "epochs": sorted(epochs),
                    }
                )
                if len(canonical(result).encode()) > 512 * 1024:
                    raise ValueError("Parent receipt provenance exceeds bounded transport capacity")
        return result

    def traffic_progress(self):
        if self.ledger.capacity_error is not None:
            raise RuntimeError("Private receipt capacity is unavailable")
        if self.traffic_source is None:
            raise RuntimeError("Private customer traffic observations are unavailable")
        facts = self.traffic_source()
        if type(facts) is not dict or set(facts) != {"running", "groups"} or type(facts["running"]) is not bool:
            raise ValueError("Malformed owner traffic facts")
        if set(facts["groups"]) != set(self.tenant_groups.values()):
            raise ValueError("Owner traffic omits a frozen database group")
        result = []
        with self.ledger._lock:
            for group, observation in sorted(facts["groups"].items()):
                if len(observation["journeys"]) > 64:
                    raise ValueError("Owner traffic observation exceeds its bounded history")
                for sequence, epoch, events in reversed(observation["journeys"]):
                    if type(sequence) is not int or sequence <= 0 or len(events) != 2:
                        raise ValueError("Malformed owner customer journey")
                    if not self.ledger._db.execute("SELECT 1 FROM epochs WHERE id=? AND closed=1", (epoch,)).fetchone():
                        continue
                    operations, effects = [], []
                    for event in events:
                        row = self.ledger._db.execute(
                            "SELECT r.body,x.effects FROM requests r JOIN expectations x USING(event_id) "
                            "WHERE r.event_id=? AND r.epoch=? AND r.status='acknowledged'",
                            (event, epoch),
                        ).fetchone()
                        if row is None:
                            raise ValueError("Completed traffic lacks an actual acknowledged owner receipt")
                        operation = operation_from_row(json.loads(row[0]))
                        if self.tenant_groups.get(operation.tenant_id) != group:
                            raise ValueError("Customer traffic differs from frozen routing")
                        operations.append(operation.observed_row())
                        effects.extend(json.loads(row[1]))
                    result.append(
                        {
                            "group": group,
                            "sequence": sequence,
                            "epoch": epoch,
                            "age_seconds": observation["age_seconds"],
                            "operations": operations,
                            "effects": effects,
                        }
                    )
                    break
        return {"running": facts["running"], "groups": result}

    def delivery_receipts(self, identities) -> dict:
        if self.observer is None or type(identities) not in (list, tuple) or not 0 < len(identities) <= 100:
            raise ValueError("Independent receiver observation needs a bounded actual-receipt batch")
        if self.observer.capacity_error is not None:
            raise RuntimeError("Trusted receiver capacity is unavailable")
        rows = self.observer.observations(tuple(identities))
        if self.observer.capacity_error is not None:
            raise RuntimeError("Trusted receiver capacity is unavailable")
        return {"receipts": list(rows)}

    def begin_epoch(self) -> int:
        epoch = self.ledger.begin_epoch()
        self._epochs.add(epoch)
        return epoch

    def _epoch(self, epoch):
        if type(epoch) is not int or epoch not in self._epochs:
            raise ValueError("Fresh IO must belong to an owner-allocated verifier epoch")

    def request(self, epoch, group, operation, effects, provenance=None) -> bool:
        self._epoch(epoch)
        op = operation_from_row(operation)
        if self.tenant_groups.get(op.tenant_id) != group:
            raise ValueError("Fresh request differs from frozen tenant routing")
        # Expected content comes from verifier-generated bytes before HTTP/Git IO.
        self.ledger.request(op, epoch, effects=effects, provenance=provenance)
        return True

    def acknowledge(self, epoch, event_id, origin, latency_ms, status_code, receipt) -> bool:
        self._epoch(epoch)
        operation = self.ledger.requested_operation(event_id, epoch)
        expected = operation.observed_row()
        digest = hashlib.sha256(canonical(expected).encode()).hexdigest()
        if (
            type(receipt) is not dict
            or any(receipt.get(key) != value for key, value in expected.items())
            or receipt.get("record_key") != operation.record_key
            or receipt.get("operation_sha256") != digest
            or type(receipt.get("replayed")) is not bool
            or type(receipt.get("region")) is not str
            or not receipt["region"]
        ):
            raise ValueError("Fresh acknowledgment differs from the full requested operation")
        self._origin(origin)
        self.ledger.acknowledge(event_id, origin, latency_ms, status_code)
        return True

    def reject(self, epoch, event_id, origin, latency_ms, status_code) -> bool:
        self._epoch(epoch)
        self.ledger.requested_operation(event_id, epoch)
        self._origin(origin)
        self.ledger.reject(event_id, origin, latency_ms, status_code)
        return True

    @staticmethod
    def _origin(origin):
        if type(origin) is not str or len(origin) > 2048:
            raise ValueError("Invalid bounded ordinary API origin")
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Invalid ordinary API origin")

    def close_epoch(self, epoch) -> bool:
        self._epoch(epoch)
        self.ledger.close_epoch(epoch)
        return True
