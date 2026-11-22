"""Conductor-only history and reference operations for airport meal clearing."""

from __future__ import annotations

import copy
import json
import logging
import shlex
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from kubernetes.client.exceptions import ApiException

PARTITIONS = range(20)
CARRIERS = ("north", "south")
CAFES = ("gate-a", "gate-b", "gate-c", "gate-d")
BANKS = ("early", "late")
LOGGER = logging.getLogger("all.sregym.airport_meal_history")
NIGHT_GROUPS = {"production": "meal-production", "rehearsal": "meal-rehearsal"}
DAY_GROUP = "meal-morning"
FIXED_GROUP = "meal-current"
ISSUER_GROUP = "meal-issuer"
ORDERS_TOPIC = "issue-orders"


@dataclass(frozen=True)
class PlannedAction:
    partition: int
    action: dict[str, Any]


def _action(
    partition: int,
    serial: int,
    carrier: str,
    cafe: str,
    bank: str,
    amount_cents: int,
    *,
    kind: str = "grant",
    revision: int = 1,
    predecessor: str | None = None,
) -> PlannedAction:
    benefit = f"{carrier[0].upper()}{serial:05d}"
    row = {
        "action_id": f"{benefit}/{revision}",
        "benefit_id": benefit,
        "carrier": carrier,
        "cafe": cafe,
        "revision": revision,
        "amount_cents": amount_cents,
        "kind": kind,
        "bank": bank,
    }
    if predecessor is not None:
        row["predecessor"] = predecessor
    return PlannedAction(partition, row)


def _fixture() -> dict[str, list[PlannedAction]]:
    """Return the deterministic partner inputs in dispatch order.

    Every partition gets four baseline grants and eight first-wave grants.
    The first wave has one two-passenger pair per carrier and departure bank.
    Even partitions update the early bank and odd partitions the late bank,
    leaving the other bank's pairs available for the old tills.
    """
    waves: dict[str, list[PlannedAction]] = {
        name: [] for name in ("baseline", "first", "offer", "second", "retained", "day")
    }
    for partition in PARTITIONS:
        for slot in range(4):
            waves["baseline"].append(
                _action(
                    partition,
                    1000 + 4 * partition + slot,
                    CARRIERS[slot // 2],
                    CAFES[(partition + slot) % 4],
                    "previous",
                    950 + 5 * partition + slot,
                )
            )
        for slot in range(8):
            carrier = CARRIERS[slot // 4]
            cafe = CAFES[(partition // 2 + slot // 2) % 4]
            bank = BANKS[(slot // 2) % 2]
            smaller = 1000 + 17 * partition + 29 * (slot // 2)
            amount = 3000 - smaller if slot % 2 else smaller
            serial = 2000 + 8 * partition + slot
            first = _action(partition, serial, carrier, cafe, bank, amount)
            waves["first"].append(first)
            if bank != BANKS[partition % 2]:
                continue
            if slot % 2 == 0:
                waves["second"].append(
                    _action(
                        partition,
                        serial,
                        carrier,
                        cafe,
                        bank,
                        amount + 80 + partition,
                        kind="revise",
                        revision=2,
                    )
                )
            else:
                waves["second"].extend(
                    (
                        _action(
                            partition,
                            serial,
                            carrier,
                            cafe,
                            bank,
                            0,
                            kind="withdraw",
                            revision=2,
                        ),
                        _action(
                            (partition + 1) % len(PARTITIONS),
                            3000 + 8 * partition + slot,
                            carrier,
                            cafe,
                            bank,
                            amount + 45,
                            predecessor=first.action["benefit_id"],
                        ),
                    )
                )
        waves["offer"].append(
            _action(
                partition,
                4000 + partition,
                CARRIERS[partition % 2],
                CAFES[partition % 4],
                BANKS[partition % 2],
                1600 + partition,
                kind="offer",
            )
        )
        for name, serial, amount in (("retained", 5000, 1220), ("day", 6000, 1340)):
            waves[name].append(
                _action(
                    partition,
                    serial + partition,
                    CARRIERS[partition % 2],
                    CAFES[partition % 4],
                    "morning",
                    amount + partition,
                )
            )

    assert Counter(row.partition for row in waves["baseline"]) == dict.fromkeys(PARTITIONS, 4)
    assert Counter(row.partition for row in waves["second"]) == dict.fromkeys(PARTITIONS, 6)
    assert Counter(row.partition for row in waves["first"]) == dict.fromkeys(PARTITIONS, 8)
    for offset in range(0, len(waves["first"]), 2):
        pair = waves["first"][offset : offset + 2]
        assert sum(row.action["amount_cents"] for row in pair) == 3000
    assert sum(row.action["amount_cents"] for row in waves["first"]) == 240_000
    return waves


def _benefit_key(action: dict[str, Any]) -> tuple[str, str]:
    return action["carrier"], action["benefit_id"]


def _current_actions(actions: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    current: dict[tuple[str, str], dict[str, Any]] = {}
    for action in actions:
        key = _benefit_key(action)
        previous = current.get(key)
        if previous is None or action["revision"] > previous["revision"]:
            current[key] = action
        elif action["revision"] == previous["revision"] and action != previous:
            raise ValueError(f"Conflicting benefit revision for {key}")
    return current


def _sale_quantities(first: list[PlannedAction], owners: Counter[int]) -> list[tuple[PlannedAction, int]]:
    """For each readable partition leave one untouched benefit unspent.

    The untouched bank's other benefits have one, all, and one numbers used,
    in north-first order. A doubled partition exercises all three cases.
    """
    plan = []
    for row in first:
        action = row.action
        partition = row.partition
        if not owners[partition] or action["bank"] == BANKS[partition % 2]:
            continue
        slot = (int(action["benefit_id"][1:]) - 2000) % 8
        count = (0, 1, owners[partition], 1)[(slot // 4) * 2 + slot % 2]
        if count:
            plan.append((row, count))
    return plan


def _allowance_for_receipt(
    benefit: tuple[str, str],
    amount_cents: int,
    allowance_cents: int,
    spent: set[tuple[str, str]],
) -> int:
    """The first accepted meal draws at most its allowance from the carrier."""
    carrier_cents = 0 if benefit in spent else min(amount_cents, allowance_cents)
    spent.add(benefit)
    return carrier_cents


# Execute ordinary HTTP requests from one pod in a batch to avoid starting
# hundreds of kubectl exec sessions. None of the fixture logic runs there.
# Setup opts in only for reads and writes whose exact retry preserves the record.
_HTTP_CLIENT = """
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import sys
import time

def send_to_site(batch):
    replies = []
    for index, (site, method, path, body, retry_transient) in batch:
        with_response = method != "POST_UNCERTAIN"
        wire = None if body is None else json.dumps(body).encode()
        attempts = 3 if retry_transient and with_response else 1
        for attempt in range(1, attempts + 1):
            connection = http.client.HTTPConnection(site, 8080, timeout=10)
            try:
                connection.request(method if with_response else "POST", path, wire,
                                   {"Content-Type": "application/json"})
                if with_response:
                    response = connection.getresponse()
                    result = [response.status, json.loads(response.read())]
                else:
                    result = [0, None]
            except (TimeoutError, ConnectionError, http.client.IncompleteRead) as error:
                if attempt == attempts:
                    raise RuntimeError(
                        f"{method} {site}:8080{path} failed after {attempt} attempt(s): {error}"
                    ) from error
            else:
                if result[0] != 503 or attempt == attempts:
                    replies.append((index, result))
                    break
            finally:
                connection.close()
            time.sleep(attempt)
    return replies

by_site = {}
for index, request in enumerate(json.load(sys.stdin)):
    by_site.setdefault(request[0], []).append((index, request))

# Keep each journal's order while allowing independent sites to write together.
with ThreadPoolExecutor(max_workers=max(1, len(by_site))) as pool:
    replies = [row for batch in pool.map(send_to_site, by_site.values()) for row in batch]
print(json.dumps([result for index, result in sorted(replies)]))
"""


class _PartnerError(RuntimeError):
    def __init__(self, site, path, status, body):
        self.status = status
        super().__init__(f"{site} {path} returned HTTP {status}: {body}")


def _clearing_client_pod(problem):
    api = problem.kubectl.core_v1_api
    namespace = problem.namespace
    endpoints = api.read_namespaced_endpoints("clearing", namespace, _request_timeout=5)
    names = {
        ref.name
        for subset in endpoints.subsets or ()
        for address in subset.addresses or ()
        if (ref := address.target_ref) is not None and ref.kind == "Pod" and ref.namespace in (None, namespace)
    }
    for name in sorted(names):
        try:
            pod = api.read_namespaced_pod(name, namespace, _request_timeout=5)
        except ApiException as exc:
            if exc.status == 404:
                continue
            raise
        if pod.metadata.deletion_timestamp is None and any(
            condition.type == "Ready" and condition.status == "True" for condition in pod.status.conditions or ()
        ):
            return name
    raise RuntimeError("The clearing Service has no Ready pod")


def _partner_batch(problem, requests, *, retry_transient, timeout=120):
    if not requests:
        return []
    pod = _clearing_client_pod(problem)
    command = shlex.join(["kubectl", "-n", problem.namespace, "exec", "-i", pod, "--", "python3", "-c", _HTTP_CLIENT])
    output = problem.kubectl.exec_command_checked(
        command,
        input_data=json.dumps([(*request, retry_transient) for request in requests]),
        timeout=timeout,
    )
    replies = json.loads(output)
    _require(len(replies) == len(requests), "Incomplete partner batch response")
    return replies


def _partner_many(problem, requests, *, retry_transient=False):
    results = []
    for (site, method, path, _), (status, body) in zip(
        requests,
        _partner_batch(problem, requests, retry_transient=retry_transient),
        strict=True,
    ):
        if status != (0 if method == "POST_UNCERTAIN" else 200):
            raise _PartnerError(site, path, status, body)
        results.append(body)
    return results


def partner_request(problem, site, method, path, body=None, *, retry_transient=False):
    """Call the same partner HTTP interface available to an operator."""
    return _partner_many(problem, [(site, method, path, body)], retry_transient=retry_transient)[0]


def _require(condition, detail):
    if not condition:
        raise RuntimeError(detail)


def _wait(description, observe, *, timeout=120, interval=2):
    deadline = time.monotonic() + timeout
    while True:
        result = observe()
        if result is not None:
            return result
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out waiting for {description}")
        time.sleep(interval)


@contextmanager
def _phase(problem, name):
    started = time.monotonic()
    evidence = {}
    status = "failed"
    try:
        yield evidence
        status = "passed"
    except Exception as exc:
        evidence["error"] = str(exc)
        raise
    finally:
        entry = {
            "name": name,
            "status": status,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            **evidence,
        }
        problem.airport_meal_history_phases.append(entry)
        LOGGER.info("Airport meals %s", entry)


def _submit(problem, rows, *, retry_transient=False):
    actions = [row.action for row in rows]
    accepted = _partner_many(
        problem,
        [(action["carrier"], "POST", "/actions", action) for action in actions],
        retry_transient=retry_transient,
    )
    _require(accepted == actions, "Carrier intake changed an action")


def _publish(problem, rows, topic_id):
    acknowledgments = problem.stream.produce([(row.partition, row.action) for row in rows])
    dispatches = []
    for row, acknowledgment in zip(rows, acknowledgments, strict=True):
        _require(
            acknowledgment["topic"] == "allowances" and acknowledgment["partition"] == row.partition,
            f"Unexpected Kafka dispatch coordinate: {acknowledgment}",
        )
        dispatches.append(
            {
                "action_id": row.action["action_id"],
                "input_topic": acknowledgment["topic"],
                "input_topic_id": topic_id,
                "input_partition": acknowledgment["partition"],
                "input_offset": acknowledgment["offset"],
            }
        )
    accepted = _partner_many(
        problem,
        [(row.action["carrier"], "POST", "/dispatches", ack) for row, ack in zip(rows, dispatches, strict=True)],
        retry_transient=True,
    )
    _require(accepted == dispatches, "Carrier dispatch journal changed a Kafka acknowledgment")
    return dispatches


def _coverage(stream, job_id, *, damaged=False):
    def observe():
        assignments = stream.assignments(job_id)
        if set(assignments) != set(PARTITIONS):
            return None
        for partition, rows in assignments.items():
            identities = {(row["subtask"], row["attempt"]) for row in rows}
            _require(len(identities) == len(rows), f"Repeated assignment identity for partition {partition}")
        counts = Counter({p: len(assignments[p]) for p in PARTITIONS})
        expected = {0: 3, 1: 14, 2: 3} if damaged else {1: 20}
        if Counter(counts.values()) == expected:
            return counts
        return None

    return _wait(f"native reader coverage of {job_id}", observe)


def _checkpoint(stream, job_id, *, newer_than=-1):
    def observe():
        checkpoint = stream.checkpoints(job_id).get("latest", {}).get("completed")
        if checkpoint and checkpoint["id"] > newer_than:
            return checkpoint["id"]
        return None

    return _wait(f"completed checkpoint of {job_id}", observe)


def _split_gauges(stream, job):
    def observe():
        metrics = stream.source_metrics(job)
        values = [
            int(float(value))
            for rows in metrics.values()
            for key, value in rows.items()
            if key.endswith("assigned-partitions")
        ]
        return metrics if len(values) == 3 and sum(values) == 20 else None

    return _wait(f"reported split gauges totaling 20 for {job}", observe)


def _input_key(row):
    return row["input_topic"], row["input_partition"], row["input_offset"]


def _output_key(row):
    return row["topic"], row["partition"], row["offset"]


def _wait_orders(stream, topic, rows, dispatches, owners, *, offsets=None, allow_other=False):
    inputs = {
        _input_key(dispatch): (row.action, owners[row.partition])
        for row, dispatch in zip(rows, dispatches, strict=True)
    }
    wanted = Counter({key: value[1] for key, value in inputs.items() if value[1]})
    action_ids = {row.action["action_id"] for row in rows}

    def observe():
        observed = stream.read_orders(topic, offsets=offsets)
        if allow_other:
            observed = [row for row in observed if row["action"]["action_id"] in action_ids]
        _require(
            len(observed) == len({_output_key(row) for row in observed}),
            f"Duplicate output coordinates in {topic} survey",
        )
        actual = Counter(_input_key(row) for row in observed)
        _require(actual <= wanted, f"Unexpected input coordinates or multiplicity in {topic}: {actual - wanted}")
        for row in observed:
            _require(row["action"] == inputs[_input_key(row)][0], f"Order payload changed in {topic}")
        return observed if actual == wanted else None

    return _wait(f"{sum(wanted.values())} orders in {topic}", observe)


def _ends(stream, topic=ORDERS_TOPIC):
    return {p: row["end"] for p, row in stream.bounds(topic).items()}


def _catalog_row(alias, action):
    return {key: alias[key] for key in ("voucher_id", "carrier", "benefit_id")} | {
        "revision": action["revision"],
        "amount_cents": action["amount_cents"],
        "active": action["kind"] != "withdraw",
    }


def _stream_order(order, input_topic_id, output_topic_id):
    topic, partition, offset = _output_key(order)
    fields = {field: order[field] for field in ("input_topic", "input_partition", "input_offset")}
    return (
        order["action"]
        | fields
        | {
            "input_topic_id": input_topic_id,
            "output_topic_id": output_topic_id,
            "output_topic": topic,
            "output_partition": partition,
            "output_offset": offset,
        }
    )


def _legacy_issue(problem, orders, topic_ids):
    delivered = _partner_many(
        problem,
        [("clearing", "POST", "/orders/legacy", _stream_order(order, *topic_ids)) for order in orders],
        retry_transient=True,
    )
    handouts, catalogs = [], []
    for row in delivered:
        if row["voucher_id"] is None:
            continue
        handouts.append(
            (row["carrier"], "POST", "/handouts", {"action_id": row["action_id"], "voucher_id": row["voucher_id"]})
        )
        catalogs.append((row["cafe"], "POST", "/catalog", _catalog_row(row, row)))
    _partner_many(problem, handouts + catalogs, retry_transient=True)
    return delivered


def _identifier(carrier, bank, suffix):
    return f"{carrier}-{bank}-{suffix}"


def _verify_originals(problem, waves, owners, additional):
    """Compare the carrier's originals to source inputs and actual reader coverage."""
    statements = {}
    late = [row for row in waves["second"] if row.action["bank"] == "late"]
    for row in waves["first"] + additional + late:
        if not owners[row.partition]:
            continue
        action = row.action
        bank_key = (action["carrier"], action["bank"])
        statements.setdefault(bank_key, {})[action["benefit_id"]] = {
            "benefit_id": action["benefit_id"],
            "revision": action["revision"],
            "amount_cents": action["amount_cents"] * owners[row.partition],
        }

    originals = _partner_many(
        problem,
        [
            (carrier, "GET", f"/statements/{_identifier(carrier, bank, 'statement')}", None)
            for carrier in CARRIERS
            for bank in BANKS
        ],
        retry_transient=True,
    )
    for original, bank_key in zip(originals, ((carrier, bank) for carrier in CARRIERS for bank in BANKS), strict=True):
        carrier, bank = bank_key
        actual = {row["benefit_id"]: row for row in original["lines"]}
        _require(
            actual == statements[bank_key],
            f"Original {carrier} {bank} statement disagrees with observed publishing coverage",
        )


def _sales(problem, first, owners, issues, first_cafe_receipts):
    issued = defaultdict(list)
    for issue in issues:
        if issue["voucher_id"] is not None:
            issued[issue["action_id"]].append(issue["voucher_id"])

    requests = []
    inputs = []
    for row, count in _sale_quantities(first, owners):
        action = row.action
        action_id = action["action_id"]
        vouchers = sorted(issued[action_id])
        _require(
            len(vouchers) == len(set(vouchers)) == owners[row.partition],
            f"Printed numbers did not match distinct issues for {action_id}",
        )
        for index, voucher in enumerate(vouchers[:count]):
            sale = {
                "request_id": f"{action['benefit_id']}-meal-{index + 1}",
                "voucher_id": voucher,
                "amount_cents": action["amount_cents"] - (100 if index == 0 else 50),
            }
            inputs.append((action, sale))
            requests.append((action["cafe"], "POST", "/receipts", sale))
    responses = _partner_many(problem, requests, retry_transient=True)
    receipts = {cafe: list(first_cafe_receipts) if cafe == CAFES[0] else [] for cafe in CAFES}
    for (action, sale), actual in zip(inputs, responses, strict=True):
        cafe = action["cafe"]
        expected = sale | {
            "cafe": cafe,
            "carrier": action["carrier"],
            "benefit_id": action["benefit_id"],
            "revision": action["revision"],
            "allowance_cents": action["amount_cents"],
            "receipt_seq": len(receipts[cafe]) + 1,
            "catalog_revision": 0,
        }
        _require(actual == expected, f"Café recorded a different sale from {sale['request_id']}")
        receipts[cafe].append(expected)
    _require(len(responses) == 54, f"Expected 54 old till sales, found {len(responses)}")

    pairs = []
    for row in first:
        action = row.action
        if (
            owners[row.partition] == 2
            and action["carrier"] == "north"
            and action["bank"] != BANKS[row.partition % 2]
            and int(action["benefit_id"][1:]) % 2 == 0
        ):
            pairs.append(
                {
                    "cafe": action["cafe"],
                    "carrier": action["carrier"],
                    "benefit_id": action["benefit_id"],
                    "amount_cents": action["amount_cents"] - 100,
                    "voucher_ids": sorted(issued[action["action_id"]]),
                }
            )
    _require(
        len(pairs) == 3 and all(len(pair["voucher_ids"]) == 2 for pair in pairs),
        "Missing unspent pairs of printed numbers",
    )

    # Every café has a real accepted receipt beyond a gap in the upload queue.
    _partner_many(
        problem, [("clearing", "POST", "/uploads", receipts[cafe][1]) for cafe in CAFES], retry_transient=True
    )
    cursors = _partner_many(
        problem,
        [("clearing", "POST", "/remittances", {"cafe": cafe}) for cafe in CAFES],
    )
    _require([row["receipt_seq"] for row in cursors] == [13, 0, 0, 0], "Remittance advanced across a receipt gap")
    return receipts, pairs[:2]


def _adjustments(problem, first, owners, current):
    missing = sorted(p for p, count in owners.items() if count == 0)
    absent = [
        current[_benefit_key(row.action)]
        for row in first
        if row.partition in missing and row.action["bank"] == "early" and int(row.action["benefit_id"][1:]) % 2 == 0
    ]
    for suffix, action, amount, status in (
        ("full", absent[0], absent[0]["amount_cents"], "posted"),
        ("partial", absent[3], 275, "posted"),
        ("queued", absent[4], 125, "pending"),
    ):
        carrier = action["carrier"]
        support = partner_request(
            problem,
            carrier,
            "POST",
            "/support",
            {
                "support_id": f"{action['benefit_id']}-{suffix}",
                "statement_id": _identifier(carrier, "early", "statement"),
                "benefit_id": action["benefit_id"],
                "revision": action["revision"],
                "amount_cents": amount,
                "status": status,
            },
            retry_transient=True,
        )
        if status == "posted":
            partner_request(
                problem, "clearing", "POST", "/support", support | {"carrier": carrier}, retry_transient=True
            )

    affected = next(row.action for row in first if owners[row.partition] == 2 and row.action["bank"] == "early")
    carrier = affected["carrier"]
    statement_id = _identifier(carrier, "early", "statement")
    account = partner_request(problem, carrier, "GET", f"/accounts/{statement_id}", retry_transient=True)
    amendment = {
        "request_id": f"{statement_id}-initial-review",
        "statement_id": statement_id,
        "expected_account_revision": account["account_revision"],
        "lines": [{"benefit_id": affected["benefit_id"], "revision": 1, "amount_cents": -75}],
    }
    partner_request(problem, carrier, "POST_UNCERTAIN", "/amendments", amendment)

    def read_ack():
        try:
            return partner_request(
                problem, carrier, "GET", f"/amendments/{amendment['request_id']}", retry_transient=True
            )
        except _PartnerError as error:
            if error.status != 404:
                raise
            return None

    applied = _wait("carrier acknowledgment of the amendment", read_ack, timeout=20)
    _require(
        applied == amendment | {"account_revision": account["account_revision"] + 1},
        "Receiver did not record the amendment whose response was lost",
    )
    _require(
        partner_request(problem, carrier, "POST", "/amendments", amendment, retry_transient=True) == applied,
        "Carrier amendment retry did not return the original result",
    )


def _shift_openings():
    return [
        _action(
            partition,
            6500 + partition,
            CARRIERS[partition % 2],
            CAFES[partition % 4],
            "morning",
            0,
            kind="offer",
        ).action
        | {
            "manifest": {
                "document": "Shift opening service roster",
                "desks": [
                    {
                        "lane": lane,
                        "desk": f"departures-{lane + 1:02d}",
                        "carrier": CARRIERS[lane % 2],
                        "cafe": CAFES[lane % 4],
                        "service": "meal voucher intake and receipt clearing",
                    }
                    for lane in PARTITIONS
                ],
            }
        }
        for partition in PARTITIONS
    ]


def _retention_cutover(stream):
    config = stream.topic_config()
    end = _ends(stream, "allowances")
    stream.alter_topic_config({"segment.ms": "1", "file.delete.delay.ms": "0"})
    openings = _shift_openings()
    receipts = stream.produce(list(zip(PARTITIONS, openings, strict=True)))
    floors = {row["partition"]: row["offset"] for row in receipts}
    _require(floors == end, "Shift openings were not written at the end of the night log")
    stream.archive("morning-openings", receipts)

    def rolled():
        segments = stream.segments()
        return segments if all(floors[p] in segments[p] for p in PARTITIONS) else None

    try:
        _wait("each shift opening to create a broker segment", rolled)
        stream.alter_topic_config({"retention.bytes": "1", "retention.ms": "-1"})

        def pruned():
            bounds = stream.bounds()
            segments = stream.segments()
            if all(
                bounds[p]["start"] == floors[p] and segments[p] == [floors[p]] for p in PARTITIONS
            ) and _old_segments_gone(stream, floors):
                return segments
            return None

        _wait("retained floors and physical night segment pruning", pruned, timeout=180)
    finally:
        overrides = {
            key: config.get(key)
            for key in (
                "segment.ms",
                "retention.bytes",
                "retention.ms",
                "file.delete.delay.ms",
            )
        }
        stream.alter_topic_config(overrides)
        steady = stream.topic_config()
        _require(
            all(steady.get(key) == value for key, value in overrides.items()),
            "Broker did not restore the steady retention settings",
        )
    return floors


def _old_segments_gone(stream, floors, topic="allowances"):
    for partition, entries in stream.segment_files(topic).items():
        for name in entries:
            base, _, suffix = name.partition(".")
            if base.isdigit() and suffix.split(".")[0] in {"log", "index", "timeindex", "txnindex"}:
                if int(base) < floors[partition]:
                    return False
    return True


def _close_preview_retention(stream):
    # Preview payloads are transient. The rehearsal deployments, assignments
    # and savepoints remain available in the ordinary job archive.
    topic = "preview-orders"
    ends = _ends(stream, topic)
    config = stream.topic_config(topic)
    overrides = {"retention.ms": "1", "file.delete.delay.ms": "0"}
    stream.alter_topic_config(overrides, topic=topic)

    def expired():
        bounds = stream.bounds(topic)
        if all(bounds[p] == {"start": end, "end": end} for p, end in ends.items()):
            if _old_segments_gone(stream, ends, topic):
                return bounds
        return None

    try:
        bounds = _wait("rehearsal preview retention", expired, timeout=90)
    finally:
        stream.alter_topic_config({key: config.get(key) for key in overrides}, topic=topic)
    _require(not stream.read_records(topic), "Temporary rehearsal output survived its retention window")
    stream.archive("preview-topic-survey", {"topic_id": stream.topic_id(topic), "bounds": bounds})


def _verify_steady_starts(stream, floors):
    bounds = stream.bounds()
    segments = stream.segments()
    _require({p: row["start"] for p, row in bounds.items()} == floors, "Retained starts moved after cutover")
    _require(
        all(segments[p] and min(segments[p]) == floors[p] for p in PARTITIONS) and _old_segments_gone(stream, floors),
        "A night segment survived the retention cutover",
    )


def _capture_expected(problem, authorizations, receipts, unused_pairs):
    from sregym.conductor.problems import airport_meal_round_two as close_history

    clearing = partner_request(problem, "clearing", "GET", "/state", retry_transient=True)
    action_by_id = {row["action_id"]: row for row in authorizations}
    handout_aliases = {}
    carriers = {}
    for carrier in CARRIERS:
        state = partner_request(problem, carrier, "GET", "/state", retry_transient=True)
        carrier_actions = sorted(
            (row for row in authorizations if row["carrier"] == carrier),
            key=lambda row: row["action_id"],
        )
        _require(
            carrier_actions
            == sorted((row for row in state["actions"] if row["kind"] != "offer"), key=lambda row: row["action_id"]),
            f"{carrier} authoritative action journal differs from submitted inputs",
        )
        carriers[carrier] = {
            key: state[key]
            for key in (
                "actions",
                "dispatches",
                "handouts",
                "closes",
                "statements",
                "support",
                "support_requests",
                "amendments",
                "exports",
                "export_approvals",
                "transfer_approvals",
                "adjustment_files",
                "file_postings",
                "counter_actions",
                "support_approvals",
            )
        }
        for handout in state["handouts"]:
            action = action_by_id[handout["action_id"]]
            alias = {key: action[key] for key in ("carrier", "benefit_id", "cafe", "revision", "amount_cents")}
            _require(alias["carrier"] == carrier, "Handout was registered at a different carrier")
            handout_aliases[handout["voucher_id"]] = alias | {"voucher_id": handout["voucher_id"]}
        editions = close_history.statement_editions(state, clearing)
        carriers[carrier]["account_editions"] = editions
        targets = close_history.statement_targets(state, clearing, editions=editions)
        carriers[carrier]["accounts"] = {
            statement_id: [
                {"benefit_id": row["benefit_id"], "revision": row["revision"], "balance_cents": row["amount_cents"]}
                for row in sorted(lines.values(), key=lambda item: item["benefit_id"])
            ]
            for statement_id, lines in targets.items()
        }
        if carrier == close_history.CARRIER:
            cases = close_history.recovery_baseline(state, clearing)

    expected_payments = []
    spent = set()
    support = defaultdict(list)
    for carrier, state in carriers.items():
        for entry in state["support"]:
            support[(carrier, entry["benefit_id"])].append(entry)
    support_release = {
        entry["support_id"]: batch["release_after"]
        for batch in cases["support_approvals"]
        for entry in batch["entries"]
    }
    for cafe, sales in receipts.items():
        for sale in sales:
            benefit = _benefit_key(sale)
            covered = _allowance_for_receipt(benefit, sale["amount_cents"], sale["allowance_cents"], spent)
            entries = sorted(
                (
                    entry
                    for entry in support[benefit]
                    if support_release.get(entry["support_id"], 0) < sale["receipt_seq"]
                ),
                key=lambda entry: entry["support_id"],
            )
            support_cents = min(covered, sum(row["amount_cents"] for row in entries))
            expected_payments.append(
                {
                    "cafe": cafe,
                    "receipt_seq": sale["receipt_seq"],
                    "carrier": sale["carrier"],
                    "benefit_id": sale["benefit_id"],
                    "amount_cents": sale["amount_cents"],
                    "carrier_cents": covered - support_cents,
                    "support_cents": support_cents,
                    "operator_cents": sale["amount_cents"] - covered,
                    "support_ids": [row["support_id"] for row in entries] if support_cents else [],
                }
            )
    paid = [row for row in expected_payments if row["cafe"] == CAFES[0] and row["receipt_seq"] <= 13]
    _require(clearing["remittances"] == paid, "Paid café rows do not agree with accepted sale inputs")
    _require(
        {row["voucher_id"]: row for row in clearing["aliases"]} == handout_aliases,
        "Printed vouchers do not agree with independent carrier handouts",
    )
    return copy.deepcopy(
        {
            "authorizations": authorizations,
            "vouchers": list(handout_aliases.values()),
            "receipts": receipts,
            "carriers": carriers,
            "receipt_payments": expected_payments,
            "remittances": paid,
            "clearing_uploads": clearing["uploads"],
            "settlement_events": clearing["settlement_events"],
            "unused_alias_pairs": unused_pairs,
            "clearing_orders": clearing["orders"],
            "departure_cases": cases,
        }
    )


def build(problem):
    """Create and verify the production incident before handing off the world."""
    from sregym.conductor.problems import airport_meal_round_two as close_history
    from sregym.conductor.problems.airport_meal_dispatch import deploy

    if problem.airport_meal_expected is not None:
        return
    _require(
        not problem.airport_meal_history_phases,
        "Airport meal setup was already attempted. Tear down the application and start a new attempt.",
    )

    stream = problem.stream
    waves = _fixture()
    all_owned = Counter(dict.fromkeys(PARTITIONS, 1))
    outputs = {"production": ORDERS_TOPIC, "rehearsal": "preview-orders"}
    with _phase(problem, "start night services") as evidence:
        # A new problem instance can also encounter a prior setup. Check before
        # deploying the broker with the short retention used for the fixture.
        existing = problem.app.command(
            "get",
            "pvc/stream-state",
            "statefulset/kafka",
            "configmap/stream-release-production",
            "configmap/stream-release-rehearsal",
            "--ignore-not-found",
            "-o",
            "name",
        )
        _require(not existing.strip(), "Remove the existing airport meal application before starting setup.")
        stream.deploy(offsets_retention_minutes=1, offsets_retention_check_interval_ms=15000)
        evidence.update(offsets_retention_minutes=1, offsets_retention_check_interval_ms=15000)
        health = _partner_many(
            problem,
            [(site, "GET", "/health", None) for site in (*CARRIERS, *CAFES, "clearing")],
            retry_transient=True,
        )
        evidence["sqlite_versions"] = sorted({row["sqlite_version"] for row in health})
        input_id = stream.topic_id()
        topic_ids = input_id, stream.topic_id(ORDERS_TOPIC)
        jobs = {
            route: stream.start(route=route, name=f"meals-{route}-evening", group=group)
            for route, group in NIGHT_GROUPS.items()
        }
        for job in jobs.values():
            _coverage(stream, job)

    with _phase(problem, "full baseline and compressed saves") as evidence:
        baseline = waves["baseline"]
        _submit(problem, baseline, retry_transient=True)
        baseline_acks = _publish(problem, baseline, input_id)
        baseline_orders = {
            route: _wait_orders(stream, topic, baseline, baseline_acks, all_owned) for route, topic in outputs.items()
        }
        baseline_end = _ends(stream, "allowances")
        before = {
            group: _wait(
                f"complete offsets for {group}",
                lambda group=group: baseline_end if stream.group_offsets(group) == baseline_end else None,
            )
            for group in NIGHT_GROUPS.values()
        }
        stream.archive("evening-group-survey", before)
        savepoints = {route: stream.stop(job, drain=False) for route, job in jobs.items()}
        stream.archive("evening-release", {"jobs": jobs, "savepoints": savepoints})
        evidence["orders"] = {route: len(rows) for route, rows in baseline_orders.items()}

    with _phase(problem, "night resize and exact ownership") as evidence:
        jobs = {
            route: stream.start(
                route=route,
                name=f"meals-{route}-overnight",
                group=group,
                parallelism=3,
                savepoint=savepoints[route],
                restore_mode="NO_CLAIM",
                compression=route == "rehearsal",
            )
            for route, group in NIGHT_GROUPS.items()
        }
        coverage = {route: _coverage(stream, job, damaged=True) for route, job in jobs.items()}
        for route, job in jobs.items():
            _checkpoint(stream, job)
            stream.archive_job(job)
            evidence[route] = dict(coverage[route])
        owners = coverage["production"]
        _legacy_issue(problem, baseline_orders["production"], topic_ids)

    with _phase(problem, "first wave customer gaps") as evidence:
        first = waves["first"]
        _submit(problem, first, retry_transient=True)
        output_before = {route: _ends(stream, topic) for route, topic in outputs.items()}
        first_output_starts = output_before
        first_acks = _publish(problem, first, input_id)
        first_orders = {
            route: _wait_orders(stream, topic, first, first_acks, coverage[route], offsets=output_before[route])
            for route, topic in outputs.items()
        }
        printed = _legacy_issue(problem, first_orders["production"], topic_ids)
        _require(
            len({row["voucher_id"] for row in printed if row["voucher_id"]}) == 160,
            "First wave did not issue 160 distinct printed numbers",
        )
        distinct = len({_input_key(row) for row in first_orders["production"]})
        cents = sum(row["amount_cents"] for row in printed)
        _require(
            len(printed) == 160 and distinct == 136 and cents == 240_000,
            "First wave did not reproduce the exact count, unique coordinates and cents",
        )
        gauges = _split_gauges(stream, jobs["production"])
        stream.archive("overnight-source-metrics", {"job_id": jobs["production"], "metrics": gauges})
        evidence.update(issued=len(printed), distinct=distinct, cents=cents, missing=24, extra=24)

    with _phase(problem, "stage departure cohorts and receiver cases"):
        rehearsal_start = _ends(stream, outputs["rehearsal"])
        cohort = close_history.prepare_cohorts(problem, owners, input_id, jobs["production"], topic_ids)
        _wait_orders(
            stream,
            outputs["rehearsal"],
            cohort["initial"] + cohort["cases"],
            cohort["dispatches"],
            coverage["rehearsal"],
            offsets=rehearsal_start,
        )

    with _phase(problem, "open offer and freeze primary export") as evidence:
        second = waves["second"] + cohort["later"]
        _submit(problem, second, retry_transient=True)
        output_before = {route: _ends(stream, topic) for route, topic in outputs.items()}
        offers = [(row.partition, row.action) for row in waves["offer"]]
        with stream.begin_transaction(offers) as transaction:
            second_acks = _publish(problem, second, input_id)
            blocked = _ends(stream, "allowances")
            tentative = {row["partition"]: row["offset"] for row in transaction.receipts}
            end_uncommitted = {p: row["end"] for p, row in stream.bounds(isolation="read_uncommitted").items()}
            _require(
                blocked == tentative
                and all(
                    blocked[ack["input_partition"]] < ack["input_offset"] < end_uncommitted[ack["input_partition"]]
                    for ack in second_acks
                ),
                "Committed second-wave records were not behind an open transaction on all lanes",
            )
            primary = close_history.capture_export(
                stream, "primary-early", ORDERS_TOPIC, jobs["production"], first_output_starts["production"]
            )
            close_history.configure_banks(problem, ("early",), primary)
            _require(
                all(_ends(stream, topic) == output_before[route] for route, topic in outputs.items()),
                "An order passed the open transaction before the early close",
            )
            stream.archive(
                "wholesale-offer-receipts",
                {
                    "receipts": transaction.receipts,
                    "committed_end": blocked,
                    "uncommitted_end": end_uncommitted,
                },
            )
            transaction.abort()
        evidence["blocked_records"] = len(second_acks)

    with _phase(problem, "abort, advance and capture late export") as evidence:
        second_orders = {
            route: _wait_orders(stream, topic, second, second_acks, coverage[route], offsets=output_before[route])
            for route, topic in outputs.items()
        }
        daily = {row.action["action_id"] for row in waves["second"]}
        _legacy_issue(
            problem, [row for row in second_orders["production"] if row["action"]["action_id"] in daily], topic_ids
        )
        late_export = close_history.capture_export(
            stream, "primary-late", ORDERS_TOPIC, jobs["production"], first_output_starts["production"]
        )
        healthy = cohort["later"][0].action
        healthy_rows = [row for row in late_export["rows"] if row["action_id"] == healthy["action_id"]]
        _require(
            len(healthy_rows) == 1
            and healthy_rows[0]["output_offset"]
            >= primary["reference"]["partitions"][str(healthy_rows[0]["output_partition"])]["end"],
            "The singly owned lane did not publish its newer edition beyond the frozen close",
        )
        stream.archive(
            "wholesale-offer-close", {"disposition": "aborted", "committed_end": _ends(stream, "allowances")}
        )
        evidence["orders"] = {route: len(rows) for route, rows in second_orders.items()}

    with _phase(problem, "expire only idle offsets and save the night release") as evidence:
        group_survey = {}
        published_through = dict.fromkeys(PARTITIONS, 0)
        for ack in second_acks:
            partition = ack["input_partition"]
            published_through[partition] = max(published_through[partition], ack["input_offset"] + 1)
        for route, group in NIGHT_GROUPS.items():
            active = {p for p, count in coverage[route].items() if count}

            def survey(group=group, active=active):
                offsets = stream.group_offsets(group)
                if offsets.keys() == active and all(offsets[p] >= published_through[p] for p in active):
                    return offsets
                return None

            group_survey[group] = _wait(f"{group} to retain only its {len(active)} active offsets", survey)
        stream.archive("night-group-survey", group_survey)
        production_savepoint = stream.stop(jobs["production"], drain=False)
        witnesses, rehearsal_savepoint = close_history.admit_contingency(
            problem, cohort, input_id, jobs["rehearsal"], first_output_starts["rehearsal"], primary, second_acks[-2:]
        )
        night_savepoints = {"production": production_savepoint, "rehearsal": rehearsal_savepoint}
        stream.archive("overnight-release", {"jobs": jobs, "savepoints": night_savepoints})
        evidence["expired"] = {
            group: sorted(set(PARTITIONS) - offsets.keys()) for group, offsets in group_survey.items()
        }

    with _phase(problem, "compile the late bank and verify original statements"):
        close_history.configure_banks(problem, ("late",), late_export)
        close_history.close_banks(problem, ("late",))
        _verify_originals(problem, waves, owners, cohort["initial"])

    authorizations = [row.action for name in ("baseline", "first", "second", "retained", "day") for row in waves[name]]
    authorizations.extend(row.action for name in ("initial", "later", "cases") for row in cohort[name])
    with _phase(problem, "café sales and carrier postings") as evidence:
        sequence, counter_action = close_history.seed_recovery_cases(problem, cohort, primary)
        authorizations.append(counter_action)
        receipts, unused_pairs = _sales(problem, first, owners, printed, sequence)
        _adjustments(problem, first, owners, _current_actions(authorizations))
        evidence["receipt_counts"] = {cafe: len(rows) for cafe, rows in receipts.items()}

    with _phase(problem, "remove night log segments and restore steady retention") as evidence:
        floors = _retention_cutover(stream)
        _close_preview_retention(stream)
        stream.set_offset_retention(10080, check_interval_ms=600000)
        _verify_steady_starts(stream, floors)
        stream.archive("morning-topic-survey", {"topic_id": input_id, "bounds": stream.bounds()})
        evidence["retained_starts"] = floors

    with _phase(problem, "morning latest start and fresh issuance") as evidence:
        _submit(problem, waves["retained"], retry_transient=True)
        retained = _publish(problem, waves["retained"], input_id)
        output_before = _ends(stream)
        _require(not stream.group_offsets(DAY_GROUP), "Morning consumer group was already in use")
        day_job = stream.start(name="meals-production-morning", group=DAY_GROUP, start="latest")
        _coverage(stream, day_job)
        _checkpoint(stream, day_job)
        _require(_ends(stream) == output_before, "Morning startup issued retained grants")
        _submit(problem, waves["day"], retry_transient=True)
        day_acks = _publish(problem, waves["day"], input_id)
        day_orders = _wait_orders(stream, ORDERS_TOPIC, waves["day"], day_acks, all_owned, offsets=output_before)
        _legacy_issue(problem, day_orders, topic_ids)
        _checkpoint(stream, day_job)
        _verify_steady_starts(stream, floors)
        _require(stream.topic_id() == input_id, "Input topic was replaced while building history")
        retained_actions = Counter(json.loads(row["value"])["action_id"] for row in stream.read_records("allowances"))
        _require(
            retained_actions
            == dict.fromkeys(
                {row.action["action_id"] for name in ("retained", "day") for row in waves[name]}
                | {row["action_id"] for row in _shift_openings()},
                1,
            ),
            "Broker retained suffix differs from the shift openings and both morning grant waves",
        )
        _require(
            stream.bounds() == stream.bounds(isolation="read_uncommitted"),
            "An open setup transaction remained at morning handoff",
        )
        state = partner_request(problem, "clearing", "GET", "/state", retry_transient=True)
        benefits = {_benefit_key(row): row for row in state["benefits"]}
        _require(
            all(_benefit_key(row.action) not in benefits for row in waves["retained"]),
            "A retained grant skipped by the latest start was fulfilled",
        )
        _require(
            all(benefits.get(_benefit_key(row.action)) == row.action for row in waves["day"]),
            "A live morning grant did not reach the issuer",
        )
        stream.archive("morning-group-survey", stream.group_offsets(DAY_GROUP))
        stream.archive_job(day_job)
        evidence.update(skipped=len(retained), issued=len(day_orders))

    with _phase(problem, "verify partner journals at handoff"):
        expected = _capture_expected(problem, authorizations, receipts, unused_pairs)
        expected["departure_witnesses"] = witnesses
        expected["aborted_action_ids"] = [row.action["action_id"] for row in waves["offer"]]
        # The history client already delivered this output range. Start the
        # live issuer at the first output it is responsible for.
        issued_through = _ends(stream)
        stream.set_group_offsets(ISSUER_GROUP, issued_through, topic=ORDERS_TOPIC)
        _require(
            stream.group_offsets(ISSUER_GROUP, topic=ORDERS_TOPIC) == issued_through,
            "Live issuer did not receive the verified morning output positions",
        )
        deploy(problem)
        _verify_steady_starts(stream, floors)
    problem.airport_meal_expected = expected


def _carrier_views(problem):
    return dict(
        zip(
            CARRIERS,
            _partner_many(
                problem,
                [(carrier, "GET", "/state", None) for carrier in CARRIERS],
            ),
            strict=True,
        )
    )


def _wait_offsets(stream, group, bounds, *, topic="allowances"):
    def observe():
        offsets = stream.group_offsets(group, topic=topic)
        return offsets if all(offsets.get(p, -1) >= end for p, end in bounds.items()) else None

    return _wait(f"{group} to consume {topic} through the handover", observe)


def _rollout(problem, command, deployment):
    prefix = ["kubectl", "-n", problem.namespace]
    problem.kubectl.exec_command_checked(shlex.join(prefix + command), timeout=60)
    problem.kubectl.exec_command_checked(
        shlex.join(prefix + ["rollout", "status", f"deployment/{deployment}", "--timeout=180s"]),
        timeout=185,
    )


def _carrier_prefix(carriers, floors, topic_id):
    historical = []
    coordinates = {}
    for state in carriers.values():
        sent = {ack["action_id"]: ack for ack in state["dispatches"]}
        for action in state["actions"]:
            ack = sent.get(action["action_id"])
            if ack is None:
                continue
            _require(
                ack["input_topic"] == "allowances" and ack.get("input_topic_id", topic_id) == topic_id,
                f"Dispatch refers to a different input topic for {action['action_id']}",
            )
            if ack["input_offset"] < floors[ack["input_partition"]]:
                historical.append(action)
                coordinates[action["action_id"]] = _input_key(ack)
    return historical, coordinates


def _verify_absent_pair(historical, coordinates, clearing, preview):
    delivered = {_input_key(row) for row in clearing["orders"] + preview}
    credited = {_benefit_key(row) for row in clearing["support"]}
    current = _current_actions(historical)
    candidates = defaultdict(list)
    for key, action in current.items():
        input_key = coordinates[action["action_id"]]
        if (
            action["bank"] in BANKS
            and action["kind"] == "grant"
            and "predecessor" not in action
            and input_key not in delivered
            and key not in credited
        ):
            candidates[(action["carrier"], action["bank"], input_key[1], action["cafe"])].append(action["amount_cents"])
    pair = next((amounts for amounts in candidates.values() if len(amounts) == len(set(amounts)) == 2), None)
    _require(pair is not None, "Carrier records did not resolve an amount allocation absent from platform records")
    # Swapping the two carrier values preserves the partition, carrier and bank totals.
    return sum(pair)


def _replay_carrier_actions(problem, historical):
    results = _partner_many(problem, [("clearing", "POST", "/orders/revision", action) for action in historical])
    handouts = []
    for action, result in zip(historical, results, strict=True):
        _require(result["action"] == action, f"Clearing changed replayed action {action['action_id']}")
        if result["voucher_id"] is not None:
            handouts.append(
                (
                    action["carrier"],
                    "POST",
                    "/handouts",
                    {
                        "action_id": action["action_id"],
                        "voucher_id": result["voucher_id"],
                    },
                )
            )
    return handouts


def _replay_suffix(problem, floors, topic_id):
    stream = problem.stream
    _require(stream.topic_id() == topic_id, "Retained positions belong to another input topic")
    retained = stream.read_records("allowances", isolation="read_committed", offsets=floors)
    rows = [PlannedAction(row["partition"], json.loads(row["value"])) for row in retained]
    acks = [
        {"input_topic": row["topic"], "input_partition": row["partition"], "input_offset": row["offset"]}
        for row in retained
    ]
    output_before = _ends(stream)
    job = stream.start(
        version="1.19.0",
        name="meals-production",
        group=FIXED_GROUP,
        start="explicit",
        offsets=floors,
        offset_reset="none",
        compression=True,
    )
    owners = _coverage(stream, job)
    _wait_orders(stream, ORDERS_TOPIC, rows, acks, owners, offsets=output_before)
    _wait_offsets(stream, ISSUER_GROUP, _ends(stream), topic=ORDERS_TOPIC)
    return job


def _close_cafes(problem):
    clearing = partner_request(problem, "clearing", "GET", "/state")
    benefits = {_benefit_key(row): row for row in clearing["benefits"]}
    for cafe in CAFES:
        state = partner_request(problem, cafe, "GET", "/state")
        existing = {row["voucher_id"]: row for row in state["catalog"]}
        target = [
            _catalog_row(alias, benefits[_benefit_key(alias)]) for alias in clearing["aliases"] if alias["cafe"] == cafe
        ]
        changes = [row for row in target if existing.get(row["voucher_id"]) != row]
        if state["mode"] == "benefit" and not state["staged"] and not changes:
            continue
        _partner_many(problem, [(cafe, "POST", "/staged", row) for row in changes])
        close = {
            "request_id": f"shift-{state['catalog_revision'] + 1}",
            "expected_catalog_revision": state["catalog_revision"],
        }
        applied = partner_request(problem, cafe, "POST", "/closes", close)
        _require(
            applied["receipt_seq"] >= state["receipt_seq"]
            and applied["catalog_revision"] == state["catalog_revision"] + 1,
            f"{cafe} did not record a durable receipt boundary",
        )
        path = f"/closes/{close['request_id']}"
        _require(
            partner_request(problem, cafe, "GET", path) == applied
            and partner_request(problem, cafe, "POST", "/closes", close) == applied,
            f"{cafe} did not preserve its shift acknowledgment for retry",
        )


def _apply_support(problem):
    for carrier, state in _carrier_views(problem).items():
        staged = {entry["support_id"] for batch in state["support_approvals"] for entry in batch["entries"]}
        posted = []
        for entry in state["support"]:
            if entry["support_id"] in staged:
                continue
            if entry["status"] == "pending":
                path = f"/support/{quote(entry['support_id'], safe='')}"
                partner_request(problem, carrier, "POST", f"{path}/post", {})
                entry = partner_request(problem, carrier, "GET", path)
                _require(entry["status"] == "posted", "Approved carrier support was not posted")
            posted.append(("clearing", "POST", "/support", entry | {"carrier": carrier}))
        _partner_many(problem, posted)


def _remit_receipts(problem):
    for cafe in CAFES:
        sales = partner_request(problem, cafe, "GET", "/receipts")
        cursor = partner_request(problem, "clearing", "GET", f"/cursors/{cafe}")["receipt_seq"]
        _partner_many(problem, [("clearing", "POST", "/uploads", row) for row in sales if row["receipt_seq"] > cursor])
        paid = partner_request(problem, "clearing", "POST", "/remittances", {"cafe": cafe})
        _require(
            paid["receipt_seq"] == max((row["receipt_seq"] for row in sales), default=0),
            f"{cafe} still has accepted receipts beyond a remittance gap",
        )


def _supplement(problem, carrier, statement, targets, *, split=False):
    statement_id = statement["statement_id"]
    key = quote(statement_id, safe="")
    path = f"/accounts/{key}"
    for _ in range(10):
        account = partner_request(problem, carrier, "GET", path)
        existing = {row["benefit_id"]: row for row in account["lines"]}
        _require(existing.keys() <= targets.keys(), f"Unexpected benefit in carrier account {statement_id}")
        revision = account["account_revision"]
        lines = []
        for benefit, target in sorted(targets.items()):
            prior = existing.get(benefit, {})
            delta = target["amount_cents"] - prior.get("balance_cents", 0)
            if delta or (prior and target["revision"] > prior["revision"]):
                lines.append({"benefit_id": benefit, "revision": target["revision"], "amount_cents": delta})
        if not lines:
            return
        if split:
            lines = lines[:1]
        correction = {
            "request_id": f"{statement_id}-supplement-{revision + 1}",
            "statement_id": statement_id,
            "expected_account_revision": revision,
            "lines": lines,
        }
        lookup = f"/amendments/{quote(correction['request_id'], safe='')}"
        try:
            applied = partner_request(problem, carrier, "POST", "/amendments", correction)
        except RuntimeError as error:
            try:
                applied = partner_request(problem, carrier, "GET", lookup, retry_transient=True)
            except _PartnerError as missing:
                if missing.status != 404:
                    raise error from missing
                if isinstance(error, _PartnerError) and error.status not in (409, 503):
                    raise error from missing
                continue
        _require(
            all(applied[key] == value for key, value in correction.items())
            and partner_request(problem, carrier, "GET", lookup) == applied
            and partner_request(problem, carrier, "POST", "/amendments", correction) == applied,
            f"{carrier} did not record the applied supplement",
        )
    raise RuntimeError(f"{carrier} account kept changing while reconciling {statement_id}")


def _supplement_statements(problem, mode=None):
    from sregym.conductor.problems.airport_meal_round_two import statement_targets

    clearing = partner_request(problem, "clearing", "GET", "/state")
    for carrier, state in _carrier_views(problem).items():
        targets = statement_targets(state, clearing)
        for original in state["statements"]:
            _supplement(
                problem,
                carrier,
                original,
                targets[original["statement_id"]],
                split=mode == "split-transfer" and original["statement_id"].endswith("-transfer-statement"),
            )


def resize_active(problem, parallelism):
    """Run the ordinary save and restore using the active release settings."""
    stream = problem.stream
    release = stream.release()
    savepoint = stream.stop(release["job_id"], drain=False)
    return stream.restore_in_place(release, savepoint, parallelism=parallelism)


def _route_partition(carrier, benefit_id):
    # The live desk dispatcher uses Kafka's keyed producer partition function.
    key = f"{carrier}\0{benefit_id}".encode()
    multiplier, mask = 0x5BD1E995, 0xFFFFFFFF
    result = 0x9747B28C ^ len(key)
    block_end = len(key) - len(key) % 4
    for index in range(0, block_end, 4):
        block = int.from_bytes(key[index : index + 4], "little") * multiplier & mask
        block = (block ^ (block >> 24)) * multiplier & mask
        result = (result * multiplier & mask) ^ block
    if block_end != len(key):
        result = (result ^ int.from_bytes(key[block_end:], "little")) * multiplier & mask
    result = (result ^ (result >> 13)) * multiplier & mask
    return ((result ^ (result >> 15)) & 0x7FFFFFFF) % len(PARTITIONS)


def _live_grants():
    routed = {}
    for serial in range(7000, 7400):
        carrier = CARRIERS[serial % 2]
        row = _action(0, serial, carrier, CAFES[serial % 4], "morning", 1520 + serial % 100)
        partition = _route_partition(carrier, row.action["benefit_id"])
        routed.setdefault(partition, PlannedAction(partition, row.action))
        if len(routed) == len(PARTITIONS):
            return [routed[p] for p in PARTITIONS]
    raise RuntimeError("Could not route a carrier intake request to every partition")


def _live_dispatches(problem, grants):
    def observe():
        carrier_views = _carrier_views(problem)
        dispatches = {row["action_id"]: row for state in carrier_views.values() for row in state["dispatches"]}
        if all(row.action["action_id"] in dispatches for row in grants):
            return [dispatches[row.action["action_id"]] for row in grants]
        return None

    result = _wait("ordinary dispatch from both carrier desks on all 20 partitions", observe)
    _require(
        [row["input_partition"] for row in result] == list(PARTITIONS),
        "Carrier dispatcher did not cover all 20 partitions",
    )
    return result


def _live_vouchers(problem, grants):
    ids = {_benefit_key(row.action) for row in grants}
    state = partner_request(problem, "clearing", "GET", "/state")
    current = {_benefit_key(action): action for action in state["benefits"]}
    _require(
        all(current.get(_benefit_key(row.action)) == row.action for row in grants),
        "Fresh authorization differs at clearing",
    )
    vouchers = defaultdict(list)
    for alias in state["aliases"]:
        if _benefit_key(alias) in ids:
            vouchers[_benefit_key(alias)].append(alias["voucher_id"])
    _require(
        set(vouchers) == ids and all(len(rows) == 1 for rows in vouchers.values()),
        "Fresh carrier authorizations did not produce one printed number each",
    )
    return {key: rows[0] for key, rows in vouchers.items()}


def _exercise_post_resize(problem, job):
    stream = problem.stream
    owners = _coverage(stream, job)
    output_begin = _ends(stream)
    input_begin = _ends(stream, "allowances")
    grants = _live_grants()
    _submit(problem, grants)
    _rollout(problem, ["scale", "deployment/meal-dispatch", "--replicas=1"], "meal-dispatch")
    _partner_many(problem, [(carrier, "POST", "/dispatch-control", {"paused": False}) for carrier in CARRIERS])
    dispatches = _live_dispatches(problem, grants)
    if all(row["input_offset"] >= input_begin[row["input_partition"]] for row in dispatches):
        _wait_orders(stream, ORDERS_TOPIC, grants, dispatches, owners, offsets=output_begin, allow_other=True)
    _wait_offsets(stream, ISSUER_GROUP, _ends(stream), topic=ORDERS_TOPIC)
    vouchers = _live_vouchers(problem, grants)

    # A normal broker redelivery has new Kafka coordinates and the same business revision.
    output_before_retry = _ends(stream)
    repeated = stream.produce([(row.partition, row.action) for row in grants])
    repeated_dispatches = [
        {"input_topic": row["topic"], "input_partition": row["partition"], "input_offset": row["offset"]}
        for row in repeated
    ]
    _wait_orders(
        stream,
        ORDERS_TOPIC,
        grants,
        repeated_dispatches,
        owners,
        offsets=output_before_retry,
        allow_other=True,
    )
    _wait_offsets(stream, ISSUER_GROUP, _ends(stream), topic=ORDERS_TOPIC)
    _require(_live_vouchers(problem, grants) == vouchers, "Replayed input printed another voucher")

    action = grants[0].action
    cafe = action["cafe"]
    sale = {
        "request_id": f"{action['benefit_id']}-morning-meal",
        "voucher_id": vouchers[_benefit_key(action)],
        "amount_cents": action["amount_cents"] - 100,
    }
    receipt = partner_request(problem, cafe, "POST", "/receipts", sale)
    _remit_receipts(problem)
    path = f"/remittances/{cafe}/{receipt['receipt_seq']}"
    payment = partner_request(problem, "clearing", "GET", path)
    _require(
        payment["carrier_cents"] == sale["amount_cents"]
        and not payment["operator_cents"]
        and not payment["support_cents"],
        "Fresh café receipt did not settle to the correct payer",
    )
    _rollout(problem, ["rollout", "restart", f"deployment/{cafe}"], cafe)
    _require(
        partner_request(problem, cafe, "POST", "/receipts", sale) == receipt,
        "Café forgot the accepted receipt after restart",
    )
    _require(
        partner_request(problem, "clearing", "POST", "/uploads", receipt) == receipt,
        "A receipt upload retry returned different contents",
    )
    _remit_receipts(problem)
    _require(partner_request(problem, "clearing", "GET", path) == payment, "Retry changed the settled receipt")


def recover(problem, *, control=None):
    """Run reference recovery for a Conductor problem that reached handoff."""
    if problem.airport_meal_expected is None:
        LOGGER.info("Airport meal handoff incomplete. Continuing with normal application teardown.")
        return
    recover_reference(problem, control=control)


def recover_reference(problem, *, control=None):
    """Reconcile the live partner records and replace the production publisher."""
    from sregym.conductor.problems import airport_meal_round_two as close_history

    stream = problem.stream
    with _phase(problem, "fence the day publisher and read original dispatches") as evidence:
        _partner_many(problem, [(carrier, "POST", "/dispatch-control", {"paused": True}) for carrier in CARRIERS])
        # Pausing allows intake to continue. Waiting for the dispatcher to stop
        # establishes that any send already in flight has finished.
        _rollout(problem, ["scale", "deployment/meal-dispatch", "--replicas=0"], "meal-dispatch")
        topic_id = stream.topic_id()
        bounds = stream.bounds()
        floors = {p: row["start"] for p, row in bounds.items()}
        _require(set(floors) == set(PARTITIONS), "Cannot establish retained positions on all 20 lanes")
        release = stream.release()
        if release["status"] == "RUNNING":
            readable = stream.read_records("allowances", isolation="read_committed", offsets=floors)
            positions = dict(floors)
            for row in readable:
                positions[row["partition"]] = max(positions[row["partition"]], row["offset"] + 1)
            if release["start"] == "latest":
                # The day release never read anything below its initial position.
                # Every lane had a served post-start grant before the handover.
                _require(
                    all(positions[p] > floors[p] for p in PARTITIONS), "Cannot establish a day publishing boundary"
                )
            _wait_offsets(stream, release["group"], positions)
            stream.stop(release["job_id"], drain=False)
        _wait_offsets(stream, ISSUER_GROUP, _ends(stream), topic=ORDERS_TOPIC)
        historical, coordinates = _carrier_prefix(_carrier_views(problem), floors, topic_id)
        clearing = partner_request(problem, "clearing", "GET", "/state")
        preview = stream.read_orders("preview-orders")
        evidence["unissued_pair_total"] = _verify_absent_pair(historical, coordinates, clearing, preview)

    with _phase(problem, "rebuild carrier prefix and replay retained input"):
        partner_request(problem, "clearing", "POST", "/issuance-cutover", {}, retry_transient=True)
        _rollout(
            problem,
            ["set", "env", "deployment/meal-issuer", "ISSUANCE_MODE=revision"],
            "meal-issuer",
        )
        handouts = _replay_carrier_actions(problem, historical)
        _replay_suffix(problem, floors, topic_id)

    with _phase(problem, "close café catalogs and settle partner journals"):
        pending = close_history.replay_counter(problem) if control != "counter" else []
        _close_cafes(problem)
        close_history.finish_counter(problem, pending)
        _partner_many(problem, handouts)
        _apply_support(problem)
        close_history.settle_approved_receipts(problem, control)
        _remit_receipts(problem)
        if control != "receiver":
            first = close_history.post_receiver(problem)
            _require(close_history.post_receiver(problem) == first, "Receiver post retry changed its acknowledgments")
        _supplement_statements(problem, control)
        if control == "untouched":
            close_history.disturb_prior_transfer(problem)

    with _phase(problem, "resize the fixed publisher and prove live café settlement"):
        job = resize_active(problem, 3)
        _exercise_post_resize(problem, job)
        close_history.verify_counter_dispatch(problem)
