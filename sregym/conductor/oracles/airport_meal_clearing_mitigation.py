"""Grade airport meal clearing through the carrier, café and release interfaces."""

import hashlib
import json
import logging
import shlex
import subprocess
import time
import uuid
from bisect import bisect_left
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum, auto
from urllib.parse import quote

from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.problems.airport_meal_history import ISSUER_GROUP, ORDERS_TOPIC, _partner_batch
from sregym.conductor.problems.airport_meal_stream import validate_release_artifact

logger = logging.getLogger(__name__)

_CARRIERS = ("north", "south")
_CAFES = ("gate-a", "gate-b", "gate-c", "gate-d")
_PARTITIONS = range(20)
_ACTION_FIELDS = (
    "action_id",
    "benefit_id",
    "carrier",
    "cafe",
    "revision",
    "amount_cents",
    "kind",
    "bank",
    "predecessor",
)
_BENEFIT_KEY = ("carrier", "benefit_id")
_REVISION_KEY = (*_BENEFIT_KEY, "revision")
_RECEIPT_KEY = ("cafe", "receipt_seq")
_SUPPORT_FIELDS = ("support_id", "statement_id", "benefit_id", "revision", "amount_cents")
_TRANSIENT_HTTP = (0, 408, 429, 500, 502, 503, 504)
_ACTION_TYPES = {key: int if key in ("revision", "amount_cents") else str for key in _ACTION_FIELDS[:-1]}
_ALIAS_TYPES = {key: _ACTION_TYPES[key] for key in (*_REVISION_KEY, "cafe", "amount_cents")} | {"voucher_id": str}
_CATALOG_TYPES = {key: kind for key, kind in _ALIAS_TYPES.items() if key != "cafe"} | {"active": bool}
_HANDOUT_TYPES = {"action_id": str, "voucher_id": str}
_RECEIPT_TYPES = _ALIAS_TYPES | {"request_id": str, "allowance_cents": int, "receipt_seq": int, "catalog_revision": int}
_READ_FIELDS = {
    "accounts": {"statement_id": str, "account_revision": int, "lines": list},
    "actions": _ACTION_TYPES,
    "adjustment-files": {"file_id": str, "lines": list},
    "aliases": _ALIAS_TYPES,
    "amendments": {"request_id": str, "statement_id": str, "account_revision": int, "lines": list},
    "benefits": _ACTION_TYPES,
    "catalog": _CATALOG_TYPES,
    "counter-actions": {"record_id": str, "signed_by": str, "action": dict},
    "counter-dispositions": {"action_id": str, "record_id": str, "disposition": str},
    "closes": {"request_id": str, "catalog_revision": int, "expected_catalog_revision": int, "receipt_seq": int},
    "cursors": {"cafe": str, "receipt_seq": int},
    "dispatches": {"action_id": str, "input_topic": str, "input_partition": int, "input_offset": int},
    "handouts": _HANDOUT_TYPES,
    "health": {"ok": bool, "role": str, "site": str},
    "issuance-cutover": {"orders_through": int},
    "orders": _ACTION_TYPES
    | {
        f"{direction}_{key}": kind
        for direction in ("input", "output")
        for key, kind in (("topic", str), ("partition", int), ("offset", int))
    }
    | {"voucher_id": (str, type(None)), "mode": str},
    "receipts": _RECEIPT_TYPES,
    "remittances": {
        key: int for key in ("receipt_seq", "amount_cents", "carrier_cents", "support_cents", "operator_cents")
    }
    | {"cafe": str, "carrier": str, "benefit_id": str, "support_ids": list},
    "requests": _RECEIPT_TYPES,
    "revisions": {"action": dict, "voucher_id": (str, type(None))},
    "state": {"role": str, "site": str},
    "support": {"support_id": str, "status": str},
    "support-requests": {"support_id": str, "status": str},
    "uploads": _RECEIPT_TYPES,
}
_STATE_COLLECTIONS = {
    "carrier": (
        "actions",
        "dispatches",
        "handouts",
        "closes",
        "statements",
        "support",
        "support_requests",
        "amendments",
        "accounts",
        "exports",
        "export_approvals",
        "transfer_approvals",
        "adjustment_files",
        "file_postings",
        "counter_actions",
        "counter_dispositions",
        "support_approvals",
    ),
    "clearing": (
        "orders",
        "revisions",
        "benefits",
        "aliases",
        "offers",
        "support",
        "uploads",
        "remittances",
        "settlement_events",
        "export_receipts",
        "cursors",
    ),
    "cafe": ("catalog", "staged", "receipts", "closes"),
}


class _BadOutcome(Exception):
    def __init__(self, reason, **detail):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class _ProbeError(RuntimeError):
    """The evaluator could not establish the expected observation boundary."""


class _ObservationUnavailable(RuntimeError):
    """The normal observation path did not return a usable business response."""

    def __init__(self, operation, **detail):
        super().__init__(operation)
        self.detail = {"operation": operation, **detail}


class _DeliveryPending(_ObservationUnavailable):
    """A delivery or its read boundary may still advance."""


class _ReadState(Enum):
    FOUND = auto()
    MISSING = auto()
    BAD_REQUEST = auto()
    TRANSIENT = auto()
    UNAVAILABLE = auto()


@dataclass(frozen=True)
class _PartnerRead:
    state: _ReadState
    status: int | None = None
    payload: object = None


@dataclass
class _Views:
    carriers: dict
    cafes: dict
    clearing: dict
    clearing_after_cafes: dict


@dataclass
class _LiveRoute:
    release: dict
    topic_id: str
    input_id: str
    start: dict
    cursor: dict
    replays: dict


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _index(rows, fields, reason):
    result = {}
    for row in rows:
        key = fields(row) if callable(fields) else tuple(row[field] for field in fields)
        if key in result:
            raise _BadOutcome(reason, duplicate=key)
        result[key] = row
    return result


def _preserved(expected, observed, fields, reason, *, immutable_fields=None):
    """Compare immutable rows (or documented immutable columns) by business identity."""
    indexed = _index(observed, fields, reason)
    for key, row in _index(expected, fields, reason).items():
        actual = indexed.get(key)
        if actual is not None:
            expected_value = row if immutable_fields is None else [row.get(field) for field in immutable_fields]
            actual_value = actual if immutable_fields is None else [actual.get(field) for field in immutable_fields]
            if _fingerprint(expected_value) == _fingerprint(actual_value):
                continue
        raise _BadOutcome(reason, record=key, missing=actual is None)
    return indexed


def _action(row):
    return {field: row[field] for field in _ACTION_FIELDS if field in row}


def _output_position(row):
    return row.get("output_topic_id", row["output_topic"]), row["output_partition"], row["output_offset"]


def _catalog_row(alias, action):
    return {
        "voucher_id": alias["voucher_id"],
        "carrier": alias["carrier"],
        "benefit_id": alias["benefit_id"],
        "revision": action["revision"],
        "amount_cents": action["amount_cents"],
        "active": action["kind"] != "withdraw",
    }


def _has_fields(row, fields):
    return isinstance(row, dict) and all(
        type(row.get(key)) in (kind if isinstance(kind, tuple) else (kind,)) and key in row
        for key, kind in fields.items()
    )


def _partner_role(site):
    if site == "clearing":
        return "clearing"
    return "carrier" if site in _CARRIERS else "cafe"


def _valid_read_payload(site, path, payload):
    collection = path.split("/")[1]
    if collection == "adjustment-files" and path.endswith("/postings"):
        return isinstance(payload, list) and all(_has_fields(row, {"line_id": str}) for row in payload)
    if not _has_fields(payload, _READ_FIELDS.get("catalog" if collection == "staged" else collection, {})):
        return False
    if collection in ("revisions", "counter-actions"):
        return _has_fields(payload["action"], _ACTION_TYPES)
    if collection != "state":
        return True
    role = _partner_role(site)
    if (payload["role"], payload["site"]) != (role, site):
        return True  # Preserve the caller's partner identity check.
    return all(
        isinstance(payload.get(name), list) and all(isinstance(row, dict) for row in payload[name])
        for name in _STATE_COLLECTIONS[role]
    ) and _has_fields(
        payload,
        {
            "carrier": {"dispatch_paused": bool},
            "clearing": {},
            "cafe": {"sales_paused": bool, "mode": str, "catalog_revision": int, "receipt_seq": int},
        }[role],
    )


def _current(actions):
    current = {}
    for row in actions:
        key = (row["carrier"], row["benefit_id"])
        if row["kind"] != "offer" and (key not in current or row["revision"] > current[key]["revision"]):
            current[key] = row
    return current


def _desk_partition(carrier, benefit_id):
    """Kafka's keyed partition for the desk's carrier and benefit key."""
    key = f"{carrier}\x00{benefit_id}".encode()
    multiplier = 0x5BD1E995
    mask = 0xFFFFFFFF
    value = 0x9747B28C ^ len(key)
    end = len(key) - len(key) % 4
    for offset in range(0, end, 4):
        block = (int.from_bytes(key[offset : offset + 4], "little") * multiplier) & mask
        block = ((block ^ (block >> 24)) * multiplier) & mask
        value = ((value * multiplier) & mask) ^ block
    if end < len(key):
        value = ((value ^ int.from_bytes(key[end:], "little")) * multiplier) & mask
    value = ((value ^ (value >> 13)) * multiplier) & mask
    return ((value ^ (value >> 15)) & 0x7FFFFFFF) % len(_PARTITIONS)


class AirportMealClearingOracle(Oracle):
    importance = 1.0
    evaluation_timeout_seconds = 600

    # Business results are only graded when an authoritative interface
    # reported them. Transport errors and broken evaluator contracts use
    # separate reasons.
    FAILURE_CLASSES = {
        "authorization_mismatch": FailureClass.AGENT_ERROR,
        "voucher_alias_mismatch": FailureClass.AGENT_ERROR,
        "receipt_history_changed": FailureClass.AGENT_ERROR,
        "receipt_payment_mismatch": FailureClass.AGENT_ERROR,
        "carrier_history_changed": FailureClass.AGENT_ERROR,
        "carrier_statement_mismatch": FailureClass.AGENT_ERROR,
        "untouched_posting_changed": FailureClass.AGENT_ERROR,
        "receiver_file_mismatch": FailureClass.AGENT_ERROR,
        "counter_withdrawal_mismatch": FailureClass.AGENT_ERROR,
        "support_release_mismatch": FailureClass.AGENT_ERROR,
        "partner_identity_mismatch": FailureClass.AGENT_ERROR,
        "voucher_use_incorrect": FailureClass.AGENT_ERROR,
        "live_issue_mismatch": FailureClass.AGENT_ERROR,
        "aborted_input_visible": FailureClass.AGENT_ERROR,
        "airport_meal_probe_error": FailureClass.HARNESS_ERROR,
        "airport_meal_observation_unavailable": FailureClass.AMBIGUOUS,
    }

    def evaluate(self) -> dict:
        self._evaluation_deadline = time.monotonic() + self.evaluation_timeout_seconds
        stage = "precheck"
        outcomes = {}
        try:
            expected = self.problem.airport_meal_expected
            if expected is None:
                raise _ProbeError("The fixture did not publish its handoff baseline")
            outcomes["before_routine"] = self.departure_outcomes(expected)
            views, release = self._precheck(expected, outcomes)
            stage = "receiver post"
            self._post_departure_file(expected)
            outcomes["after_receiver_post"] = self.departure_outcomes(expected, posted=True)
            for result in outcomes["after_receiver_post"].values():
                if not result["success"]:
                    return self.fail(result["reason"], stage=stage, departure_outcomes=outcomes)
            views = self._history_views(expected)
            stage = "live release and partner operations"
            self._exercise(expected, views, release)
            outcomes["after_resize_and_close"] = self.departure_outcomes(expected, posted=True)
            for result in outcomes["after_resize_and_close"].values():
                if not result["success"]:
                    return self.fail(result["reason"], stage=stage, departure_outcomes=outcomes)
        except _BadOutcome as exc:
            logger.info("Airport meal clearing %s failed: %s %s", stage, exc.reason, exc.detail)
            return self.fail(exc.reason, stage=stage, departure_outcomes=outcomes, **exc.detail)
        except (_ProbeError, KeyError, TypeError, ValueError) as exc:
            logger.exception("Airport meal clearing probe failed during %s", stage)
            return self.fail("airport_meal_probe_error", stage=stage, departure_outcomes=outcomes, error=str(exc))
        except _ObservationUnavailable as exc:
            logger.info("Airport meal clearing observation unavailable during %s: %s", stage, exc)
            return self.fail(
                "airport_meal_observation_unavailable", stage=stage, departure_outcomes=outcomes, **exc.detail
            )
        except Exception as exc:
            logger.exception("Could not evaluate airport meal clearing during %s", stage)
            return self.fail_from_exception(exc, stage=stage, departure_outcomes=outcomes)
        return {"success": True, "departure_outcomes": outcomes}

    def departure_outcomes(self, expected, *, posted=False):
        """Read each case through its own interfaces so all outcomes are reported."""
        case = expected["departure_cases"]
        baseline = expected["carriers"]["north"]
        outcomes = {}
        checks = {
            "untouched_transfer": lambda: self._check_transfer(case, baseline),
            "receiver_file": lambda: self._check_receiver(case, baseline, posted),
            "counter_withdrawal": lambda: self._check_counter(case),
            "support_release": lambda: self._check_support_release(case, expected),
        }
        for name, check in checks.items():
            try:
                check()
            except _BadOutcome as exc:
                outcomes[name] = {"success": False, "reason": exc.reason, **exc.detail}
            except _ObservationUnavailable as exc:
                outcomes[name] = {"success": False, "reason": "airport_meal_observation_unavailable", **exc.detail}
            except (KeyError, TypeError, _ProbeError) as exc:
                outcomes[name] = {"success": False, "reason": "airport_meal_probe_error", "error": str(exc)}
            else:
                outcomes[name] = {"success": True}
        return outcomes

    def _check_case_account(self, baseline, view, statement_id, reason):
        original = next(row for row in baseline["statements"] if row["statement_id"] == statement_id)
        try:
            self._check_account("north", original, baseline["accounts"][statement_id], view, baseline=baseline)
        except _BadOutcome as exc:
            raise _BadOutcome(reason, **exc.detail) from exc

    def _check_transfer(self, case, baseline):
        self._check_case_account(
            baseline, self._read("north", "/state"), case["prior_transfer"]["statement_id"], "untouched_posting_changed"
        )

    def _check_receiver(self, case, baseline, posted):
        reason = "receiver_file_mismatch"
        view = self._read("north", "/state")
        (original,) = baseline["adjustment_files"]
        filename = original["file_id"]
        self._check_case_account(baseline, view, original["statement_id"], reason)
        _preserved([original], view["adjustment_files"], ("file_id",), reason)
        rows = [row for row in view["file_postings"] if row["file_id"] == filename]
        identities = [row["line_id"] for row in original["lines"]]
        _preserved(case["postings"], rows, ("file_id", "line_id"), reason)
        if [row["line_id"] for row in rows] != identities[: len(rows)] or (posted and len(rows) != len(identities)):
            raise _BadOutcome(reason, file_id=filename, view="receiver posting order")
        amendments = _index(view["amendments"], ("request_id",), reason)
        for row, line in zip(rows, original["lines"], strict=False):
            entry = amendments.get((row["posting_id"],))
            if (
                entry is None
                or entry["statement_id"] != original["statement_id"]
                or entry["lines"] != [{key: value for key, value in line.items() if key != "line_id"}]
            ):
                raise _BadOutcome(reason, file_id=filename, line_id=row["line_id"])

    def _post_departure_file(self, expected):
        filename = expected["departure_cases"]["filename"]
        path = f"/adjustment-files/{quote(filename, safe='')}"
        try:
            status, applied = self._request("north", f"{path}/post", {})
        except _ObservationUnavailable:
            stored = self._optional_read("north", f"{path}/postings")
            original = self._optional_read("north", path)
            if (
                stored is not None
                and original is not None
                and [row["line_id"] for row in stored] == [row["line_id"] for row in original["lines"]]
            ):
                return stored
            raise
        if status == 400:
            raise _ProbeError(f"The receiver rejected the post probe input: {applied}")
        if status != 200:
            raise _BadOutcome("receiver_file_mismatch", file_id=filename, status=status, response=applied)
        if not isinstance(applied, list):
            raise _ProbeError("The receiver did not return its keyed posting acknowledgments")
        return applied

    def _check_counter(self, case):
        reason = "counter_withdrawal_mismatch"
        counter = case["counter"]
        action = counter["action"]
        key = action["action_id"]
        voucher = case["supplied"]["voucher_id"]
        carrier = self._read("north", "/state")
        clearing = self._read("clearing", "/state")
        _preserved([counter], carrier["counter_actions"], lambda row: row["action"]["action_id"], reason)
        _preserved([action], carrier["actions"], ("action_id",), reason)
        _preserved([case["supplied"]], carrier["handouts"], ("voucher_id",), reason)
        _preserved(
            [{"action_id": key, "record_id": counter["record_id"], "disposition": "fulfilled-locally"}],
            carrier["counter_dispositions"],
            ("action_id",),
            reason,
        )
        revision = self._read(
            "clearing", self._key_path("revisions", "north", action["benefit_id"], action["revision"]), reason=reason
        )
        if revision != {"action": action, "voucher_id": None}:
            raise _BadOutcome(reason, action_id=key, view="recorded counter revision")
        self._counter_catalog(action, voucher)
        if any(row["action_id"] == key for row in carrier["dispatches"] + clearing["orders"]):
            raise _BadOutcome(reason, action_id=key, view="unexpected ordinary dispatch")

    def _check_support_release(self, case, expected):
        reason = "support_release_mismatch"
        carrier = self._read("north", "/state")
        clearing = self._read("clearing", "/state")
        cafe = case["support_approvals"][0]["cafe"]
        cafe_state = self._read(cafe, "/state")
        _preserved(case["support_approvals"], carrier["support_approvals"], ("approval_id",), reason)
        keys = {(cafe, sequence) for sequence in (14, 15, 16)}
        _preserved(
            [row for row in expected["receipts"][cafe] if (cafe, row["receipt_seq"]) in keys],
            cafe_state["receipts"],
            _RECEIPT_KEY,
            reason,
        )
        _preserved(
            [row for row in expected["receipt_payments"] if (row["cafe"], row["receipt_seq"]) in keys],
            clearing["remittances"],
            _RECEIPT_KEY,
            reason,
        )
        payments = {
            (row["cafe"], row["receipt_seq"]): row["sequence"]
            for row in clearing["settlement_events"]
            if row["kind"] == "payment"
        }
        acknowledgments = _index(clearing["remittances"], _RECEIPT_KEY, reason)
        credits = {
            row["support_id"]: row["sequence"]
            for row in clearing["settlement_events"]
            if row["kind"] == "support" and row["carrier"] == "north"
        }
        for batch in case["support_approvals"]:
            boundary = batch["release_after"]
            payment = acknowledgments.get((cafe, boundary))
            if payment is None or (cafe, boundary) not in payments or (cafe, boundary + 1) not in payments:
                raise _BadOutcome(reason, cafe=cafe, receipt_seq=boundary, view="release payment missing")
            for entry in batch["entries"]:
                support_id = entry["support_id"]
                if (
                    support_id not in credits
                    or not payments[(cafe, boundary)] < credits[support_id] < payments[(cafe, boundary + 1)]
                ):
                    raise _BadOutcome(reason, cafe=cafe, support_id=support_id, release_after=boundary)
                posted = self._read("north", f"/support/{quote(support_id, safe='')}", reason=reason)
                if posted.get("release_payment") != payment:
                    raise _BadOutcome(reason, cafe=cafe, support_id=support_id, view="carrier release payment")
                _preserved([posted | {"carrier": "north"}], clearing["support"], ("carrier", "support_id"), reason)

    def _request_once(self, site, method, path, request, *, timeout=None):
        # The Service proxy can decode an escaped slash before the partner
        # splits the path. Send encoded keys as-is through the in-cluster client.
        if "%" in path:
            [(status, payload)] = _partner_batch(
                self.problem, [(site, method, path, request)], retry_transient=False, timeout=timeout or 15
            )
            return status, payload

        api = self.problem.kubectl.core_v1_api.api_client
        namespace = self.problem.namespace
        service_path = f"/api/v1/namespaces/{namespace}/services/http:{site}:8080/proxy{path}"
        try:
            return 200, api.call_api(
                service_path,
                method,
                body=request,
                header_params={"Accept": "application/json", "Content-Type": "application/json"},
                response_type="object",
                auth_settings=["BearerToken"],
                _return_http_data_only=True,
                _request_timeout=(5, 10) if timeout is None else timeout,
            )
        except ApiException as exc:
            payload = exc.body or str(exc)
            if exc.status in (400, 404, 409):
                with suppress(TypeError, ValueError):
                    payload = json.loads(payload)
            return exc.status, payload

    def _request(self, site, path, request=None):
        if request is None:
            result = self._get(site, path)
            return result.status, result.payload
        operation = f"POST {site} {path}"
        for attempt in range(3):
            try:
                status, payload = self._request_once(site, "POST", path, request)
            except (ApiException, HTTPError, OSError, RuntimeError, ValueError) as exc:
                if attempt == 2:
                    raise _ObservationUnavailable(operation, error=str(exc)) from exc
            else:
                if status == 200 or (
                    status in (400, 404, 409) and isinstance(payload, dict) and isinstance(payload.get("error"), str)
                ):
                    return status, payload
                if attempt == 2 or status not in _TRANSIENT_HTTP:
                    raise _ObservationUnavailable(operation, status=status, error=payload)
            time.sleep(0.5)

    def _read_result(self, site, path, *, timeout=None):
        try:
            if timeout is None:
                status, payload = self._request_once(site, "GET", path, None)
            else:
                status, payload = self._request_once(site, "GET", path, None, timeout=timeout)
        except (ApiException, HTTPError, OSError, RuntimeError, TypeError, ValueError) as exc:
            return _PartnerRead(_ReadState.UNAVAILABLE, payload=str(exc))
        if status == 200 and _valid_read_payload(site, path, payload):
            return _PartnerRead(_ReadState.FOUND, status, payload)
        if status in _TRANSIENT_HTTP:
            return _PartnerRead(_ReadState.TRANSIENT, status, payload)
        error = payload.get("error") if isinstance(payload, dict) else None
        collection = path.split("/")[1].replace("-", "_")
        missing = {f"{collection} record not found"}
        if collection == "requests":
            missing = {"_accepted_requests record not found", "receipts record not found"}
        if collection == "issuance_cutover":
            missing = {"_settings record not found"}
        if status == 404 and isinstance(error, str) and error in missing:
            return _PartnerRead(_ReadState.MISSING, status, payload)
        if status == 400 and isinstance(error, str):
            return _PartnerRead(_ReadState.BAD_REQUEST, status, payload)
        return _PartnerRead(_ReadState.UNAVAILABLE, status, payload)

    def _get(self, site, path):
        for attempt in range(3):
            result = self._read_result(site, path)
            if result.state is _ReadState.BAD_REQUEST:
                raise _ProbeError(f"GET {site} {path} rejected the probe input: {result.payload}")
            if result.state in (_ReadState.FOUND, _ReadState.MISSING):
                return result
            if attempt < 2:
                time.sleep(0.5)
        raise _ObservationUnavailable(f"GET {site} {path}", status=result.status, response=result.payload)

    def _optional_read(self, site, path):
        result = self._get(site, path)
        return result.payload if result.state is _ReadState.FOUND else None

    def _read(self, site, path, *, reason=None):
        result = self._get(site, path)
        if result.state is _ReadState.FOUND:
            row = result.payload
            if path == "/state" and (row["role"], row["site"]) != (_partner_role(site), site):
                raise _BadOutcome("partner_identity_mismatch", site=site, role=row["role"], served_by=row["site"])
            return row
        if reason is not None:
            raise _BadOutcome(reason, site=site, path=path, status=result.status, response=result.payload)
        raise _ObservationUnavailable(f"GET {site} {path}", status=result.status, response=result.payload)

    def _read_views(self):
        # The first clearing read bounds settled receipts at the tills.
        # The second bounds clearing delivery and carrier authorization.
        clearing = self._read("clearing", "/state")
        cafes = {site: self._read(site, "/state") for site in _CAFES}
        clearing_after_cafes = self._read("clearing", "/state")
        carriers = {site: self._read(site, "/state") for site in _CARRIERS}
        return _Views(carriers, cafes, clearing, clearing_after_cafes)

    def _precheck(self, expected, outcomes):
        for result in outcomes["before_routine"].values():
            if not result["success"]:
                detail = {key: value for key, value in result.items() if key not in ("success", "reason")}
                if result["reason"] == "airport_meal_observation_unavailable":
                    raise _ObservationUnavailable(**detail)
                if result["reason"] == "airport_meal_probe_error":
                    raise _ProbeError(detail["error"])
                raise _BadOutcome(result["reason"], **detail)
        stream = self.problem.stream
        release = self._production_release()
        available = self._stream_call(stream.check_release_artifact, release)
        if type(available) is not bool:
            raise _ProbeError("Invalid production artifact observation")
        if not available:
            raise _BadOutcome("live_issue_mismatch", job_id=release["job_id"], view="missing production JAR")
        self._require_reader_ownership(stream, release["job_id"])
        views = self._history_views(expected)
        return views, release

    def _history_views(self, expected, *, actions=(), receipts=()):
        deadline = self._deadline(30)
        previous = None
        while True:
            try:
                views = self._read_views()
                self._check_history(expected, views, actions=actions, receipts=receipts)
            except _DeliveryPending as exc:
                previous = None
                pending = exc
            except _BadOutcome as exc:
                if previous == (exc.reason, exc.detail):
                    raise
                previous = (exc.reason, exc.detail)
                pending = _ObservationUnavailable("consistent partner history", last_result=previous)
            else:
                return views
            self._wait_for_partner(deadline, pending)

    def _deadline(self, timeout):
        return min(time.monotonic() + timeout, getattr(self, "_evaluation_deadline", float("inf")))

    @staticmethod
    def _wait_for_partner(deadline, error):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise error
        time.sleep(min(1, remaining))

    def _check_history(self, expected, views, *, actions=(), receipts=()):
        required = [*expected["authorizations"], *actions]
        observed = self._check_carriers(expected, views.carriers, required)
        aliases = self._check_vouchers(expected, required, observed, views)
        self._check_receipts(expected, observed, aliases, views, receipts)
        if cases := expected.get("departure_cases"):
            _preserved(
                cases["export_receipts"],
                views.clearing["export_receipts"],
                ("receipt_id",),
                "carrier_history_changed",
            )
        self._check_aborted(expected.get("aborted_action_ids", ()), views)
        for cafe, view in views.cafes.items():
            if view.get("sales_paused"):
                raise _BadOutcome("voucher_use_incorrect", cafe=cafe, view="sales paused")

    def _check_carriers(self, expected, carriers, required):
        actions = []
        for carrier, view in carriers.items():
            baseline = expected["carriers"][carrier]
            expected_actions = [row for row in required if row["carrier"] == carrier]
            _preserved(
                expected_actions,
                view["actions"],
                ("action_id",),
                "authorization_mismatch",
                immutable_fields=_ACTION_FIELDS,
            )
            if any(row["carrier"] != carrier for row in view["actions"]):
                raise _BadOutcome("authorization_mismatch", carrier=carrier)
            actions.extend(view["actions"])
            for collection, identity in (
                ("dispatches", "action_id"),
                ("handouts", "voucher_id"),
                ("closes", "close_id"),
                ("statements", "statement_id"),
                ("support_requests", "support_id"),
                ("amendments", "request_id"),
                ("export_approvals", "approval_id"),
                ("transfer_approvals", "approval_id"),
                ("adjustment_files", "file_id"),
                ("support_approvals", "approval_id"),
            ):
                _preserved(baseline[collection], view[collection], (identity,), "carrier_history_changed")
            _preserved(
                baseline["exports"],
                view["exports"],
                lambda row: row["reference"]["range_id"],
                "carrier_history_changed",
            )
            _preserved(
                baseline.get("file_postings", ()),
                view["file_postings"],
                ("file_id", "line_id"),
                "carrier_history_changed",
            )
            for collection, key in (
                ("statements", "statement_id"),
                ("support_requests", "support_id"),
                ("amendments", "request_id"),
            ):
                earlier = [row[key] for row in baseline[collection]]
                if [row[key] for row in view[collection]][: len(earlier)] != earlier:
                    raise _BadOutcome("carrier_history_changed", carrier=carrier, view=f"{collection} order")
            requests = _index(view["support_requests"], ("support_id",), "carrier_history_changed")
            support = _preserved(
                baseline["support"],
                view["support"],
                ("support_id",),
                "carrier_history_changed",
                immutable_fields=_SUPPORT_FIELDS,
            )
            if support.keys() != requests.keys():
                raise _BadOutcome("carrier_history_changed", carrier=carrier, view="support request journal")
            for key, row in support.items():
                if any(row[field] != requests[key][field] for field in _SUPPORT_FIELDS) or (
                    row["status"] == "pending" and requests[key]["status"] != "pending"
                ):
                    raise _BadOutcome("carrier_history_changed", carrier=carrier, support_id=key[0])
            for row in baseline["support"]:
                current = support[(row["support_id"],)]
                if row["status"] == "posted" and current != row:
                    raise _BadOutcome("carrier_history_changed", carrier=carrier, support_id=row["support_id"])
                if current["status"] != "posted":
                    raise _BadOutcome("carrier_statement_mismatch", carrier=carrier, support_id=row["support_id"])
            if view["dispatch_paused"]:
                raise _BadOutcome("live_issue_mismatch", carrier=carrier, view="dispatch paused")
            for statement_id, target in baseline["accounts"].items():
                original = next(row for row in baseline["statements"] if row["statement_id"] == statement_id)
                self._check_account(carrier, original, target, view, baseline=baseline)
        _index([row for row in actions if row["kind"] != "offer"], _REVISION_KEY, "authorization_mismatch")
        return actions

    @staticmethod
    def _check_account(carrier, original, target, view, *, baseline=None):
        statement_id = original["statement_id"]
        detail = {"carrier": carrier, "statement_id": statement_id}
        reason = "carrier_statement_mismatch"
        account = _index(view["accounts"], ("statement_id",), reason).get((statement_id,))
        if account is None:
            raise _BadOutcome(reason, **detail)

        calculated = defaultdict(lambda: {"revision": 0, "original_cents": 0, "support_cents": 0, "amended_cents": 0})

        def apply(lines, column):
            for line in lines:
                balance = calculated[line["benefit_id"]]
                balance[column] += line["amount_cents"]
                balance["revision"] = max(balance["revision"], line["revision"])

        apply(original["lines"], "original_cents")
        support = [row for row in view["support"] if row["statement_id"] == statement_id and row["status"] == "posted"]
        amendments = [row for row in view["amendments"] if row["statement_id"] == statement_id]
        for row in support:
            apply([row], "support_cents")
        for row in amendments:
            apply(row["lines"], "amended_cents")
            if row["expected_account_revision"] + 1 != row["account_revision"]:
                raise _BadOutcome(reason, amendment=row["request_id"], **detail)
        amendment_versions = [row["account_revision"] for row in amendments]
        versions = amendment_versions + [row["account_revision"] for row in support]
        if (
            amendment_versions != sorted(amendment_versions)
            or sorted(versions) != list(range(1, len(versions) + 1))
            or account["account_revision"] != len(versions)
        ):
            raise _BadOutcome(reason, view="account posting order", **detail)

        observed = _index(account["lines"], ("benefit_id",), reason)
        if set(observed) != {(benefit,) for benefit in calculated}:
            raise _BadOutcome(reason, **detail)
        for benefit, amounts in calculated.items():
            amounts["balance_cents"] = amounts["original_cents"] + amounts["support_cents"] + amounts["amended_cents"]
            actual = observed[(benefit,)]
            if any(actual[field] != value for field, value in amounts.items()):
                raise _BadOutcome(reason, benefit_id=benefit, **detail)

        targets = _index(target, ("benefit_id",), reason)
        if baseline is not None:
            targets = AirportMealClearingOracle._account_authorized_target(
                original, targets, baseline, view, amendments, support, detail
            )
        for key in targets.keys() | observed.keys():
            wanted = targets.get(key)
            actual = observed.get(key)
            if wanted is None and actual["balance_cents"] == 0:
                continue
            if actual is None and wanted["balance_cents"] == 0:
                continue
            if (
                wanted is None
                or actual is None
                or any(actual[field] != wanted[field] for field in ("revision", "balance_cents"))
            ):
                raise _BadOutcome(reason, benefit_id=key[0], **detail)

    @staticmethod
    def _account_authorized_target(original, targets, baseline, view, amendments, support, detail):
        """Keep the handoff repair separate from later, independently authorized postings."""
        statement_id = original["statement_id"]
        reason = "carrier_statement_mismatch"
        targets = {key: row.copy() for key, row in targets.items()}

        def apply(ledger, line):
            key = (line["benefit_id"],)
            row = ledger.setdefault(key, {"revision": 0, "balance_cents": 0})
            row["revision"] = max(row["revision"], line["revision"])
            row["balance_cents"] += line["amount_cents"]

        def reduces_residual(amount, residual):
            return 0 < amount <= residual or residual <= amount < 0

        originals = [row for row in baseline["amendments"] if row["statement_id"] == statement_id]
        original_ids = {row["request_id"] for row in originals}
        _preserved(originals, amendments, ("request_id",), reason)
        if amendments[: len(originals)] != originals:
            raise _BadOutcome(reason, view="original posting order", **detail)
        original_support = [row for row in baseline["support"] if row["statement_id"] == statement_id]
        support_ids = {row["support_id"] for row in original_support}
        _preserved([row for row in original_support if row["status"] == "posted"], support, ("support_id",), reason)

        handoff = {}
        for line in [*original["lines"], *(line for row in originals for line in row["lines"]), *original_support]:
            apply(handoff, line)
        remaining = {}
        repair_revisions = {}
        posted_revisions = {key: row["revision"] for key, row in handoff.items()}
        for key in targets.keys() | handoff.keys():
            prior = handoff.get(key)
            wanted = targets.get(key)
            if wanted is None:
                targets[key] = wanted = {"revision": prior["revision"], "balance_cents": 0}
            if prior is not None:
                wanted["revision"] = max(prior["revision"], wanted["revision"])
            if prior is None and wanted["balance_cents"] == 0:
                continue
            if prior is None or any(prior[field] != wanted[field] for field in ("revision", "balance_cents")):
                repair_revisions[key] = wanted["revision"]
                remaining[key] = wanted["balance_cents"] - (prior["balance_cents"] if prior is not None else 0)

        actions = [row for row in view["actions"] if row["kind"] != "offer"]
        authorized = _index(actions, ("benefit_id", "revision"), "authorization_mismatch")
        original_actions = [row for row in baseline.get("actions", actions) if row["kind"] != "offer"]
        sealed_authority = _index(original_actions, ("benefit_id", "revision"), "authorization_mismatch")
        sealed_action_ids = {row["action_id"] for row in original_actions}
        sealed_revisions = {row["benefit_id"]: row["revision"] for row in _current(original_actions).values()}
        bank = next(row["bank"] for row in baseline["closes"] if row["close_id"] == original["close_id"])
        editions = baseline.get("account_editions", {}).get(statement_id, {})
        # A zeroed export line can also mean the cohort moved off this account.
        desk_editions = {
            benefit: row
            for benefit, row in editions.items()
            if (authority := sealed_authority.get((benefit, row["revision"]))) is not None
            and authority["bank"] == bank
            and authority["amount_cents"] == row["amount_cents"]
        }

        prior_approvals = {row["approval_id"] for row in baseline["transfer_approvals"]}

        # Historical targets already include these signed shifts. Reserve
        # the legs in the handoff journal or assigned to an accepted file.
        def transfer_key(line):
            return line["benefit_id"], line["revision"], line["amount_cents"]

        receiver_postings = {row["posting_id"] for row in baseline.get("file_postings", ())}
        reserved_legs = Counter(
            (row.get("transfer_approval_id"), *transfer_key(line))
            for row in originals
            if row["request_id"] not in receiver_postings
            for line in row["lines"]
        )
        reserved_legs.update(
            (None, *transfer_key(line))
            for file in baseline["adjustment_files"]
            if file["statement_id"] == statement_id
            for line in file["lines"]
        )
        pending_transfers = {}
        for approval in baseline["transfer_approvals"]:
            if approval["statement_id"] != statement_id or not approval["signed_by"]:
                continue
            approval_id = approval["approval_id"]
            for line in approval["lines"]:
                if reserved_legs[(approval_id, *transfer_key(line))]:
                    continue
                ordinary = (None, *transfer_key(line))
                if reserved_legs[ordinary]:
                    reserved_legs[ordinary] -= 1
                    continue
                pending_transfers[(approval_id, line["benefit_id"])] = line

        signed_transfers = {
            (row["approval_id"], line["benefit_id"]): line
            for row in view["transfer_approvals"]
            if row["statement_id"] == statement_id and row["approval_id"] not in prior_approvals and row["signed_by"]
            for line in row["lines"]
        }
        signed_support = {
            line["support_id"]: (batch, line)
            for batch in view["support_approvals"]
            if batch["signed_by"]
            for line in batch["entries"]
            if line["statement_id"] == statement_id
        }
        linked = {
            (row["transfer_approval_id"], line["benefit_id"])
            for row in amendments
            if row.get("transfer_approval_id")
            for line in row["lines"]
        }
        unlinked = defaultdict(list)
        for approval, line in signed_transfers.items():
            if approval not in linked:
                unlinked[(line["benefit_id"], line["revision"], line["amount_cents"])].append(approval)

        accepted_files = {
            (file["file_id"], line["line_id"]): {field: value for field, value in line.items() if field != "line_id"}
            for file in baseline["adjustment_files"]
            if file["statement_id"] == statement_id
            for line in file["lines"]
        }
        receiver_lines = {
            row["posting_id"]: accepted_files[(row["file_id"], row["line_id"])]
            for row in view["file_postings"]
            if (row["file_id"], row["line_id"]) in accepted_files
        }
        for row in support:
            if row["support_id"] in support_ids:
                continue
            approval, entry = signed_support.get(row["support_id"], (None, None))
            payment = row.get("release_payment", {})
            key = (row["benefit_id"], row["revision"])
            if (
                entry is None
                or any(entry[field] != row[field] for field in _SUPPORT_FIELDS)
                or key not in authorized
                or authorized[key]["cafe"] != approval["cafe"]
                or (payment.get("cafe"), payment.get("receipt_seq")) != (approval["cafe"], approval["release_after"])
            ):
                raise _BadOutcome(reason, support_id=row["support_id"], view="unsigned support posting", **detail)
            apply(targets, row)

        for row in amendments:
            posting = row["request_id"]
            if posting in original_ids:
                continue
            if posting in receiver_lines and row["lines"] == [receiver_lines[posting]]:
                line = receiver_lines[posting]
                key = (line["benefit_id"],)
                remaining[key] = remaining.get(key, 0) - line["amount_cents"]
                continue
            for line in row["lines"]:
                benefit = line["benefit_id"]
                key = (benefit,)
                revision = line["revision"]
                amount = line["amount_cents"]
                residual = remaining.get(key, 0)
                authority = authorized.get((benefit, revision))
                approval_id = row.get("transfer_approval_id")
                if approval_id is not None:
                    transfer = (approval_id, benefit)
                    if (
                        authority is not None
                        and pending_transfers.get(transfer) == line
                        and reduces_residual(amount, residual)
                    ):
                        del pending_transfers[transfer]
                        remaining[key] = residual - amount
                        posted_revisions[key] = max(revision, posted_revisions.get(key, 0))
                        continue
                    if signed_transfers.pop(transfer, None) == line and authority is not None:
                        apply(targets, line)
                        continue
                elif repair_revisions.get(key) == revision and (
                    reduces_residual(amount, residual)
                    or (amount == residual == 0 and revision > posted_revisions.get(key, 0))
                ):
                    remaining[key] = residual - amount
                    posted_revisions[key] = revision
                    continue
                elif matches := unlinked[(benefit, revision, amount)]:
                    if authority is not None:
                        signed_transfers.pop(matches.pop())
                        apply(targets, line)
                        continue
                elif (
                    authority is not None
                    and authority["action_id"] not in sealed_action_ids
                    and authority["carrier"] == detail["carrier"]
                    and authority["bank"] == bank
                    and (prior := desk_editions.get(benefit)) is not None
                    and revision > max(prior["revision"], sealed_revisions[benefit])
                ):
                    # A later charge can be posted together with the outstanding historical repair.
                    delta = authority["amount_cents"] - prior["amount_cents"]
                    due = residual + delta
                    if amount == due or (amount == 0 and delta == 0) or reduces_residual(amount, due):
                        desk_editions[benefit] = authority
                        apply(targets, line | {"amount_cents": delta})
                        remaining[key] = due - amount
                        repair_revisions[key] = posted_revisions[key] = revision
                        continue
                raise _BadOutcome(reason, benefit_id=benefit, posting_id=posting, view="unapproved posting", **detail)
        return targets

    def _check_vouchers(self, expected, required, actions, views):
        reason = "voucher_alias_mismatch"
        required_current = _current(required)
        known = _index([row for row in actions if row["kind"] != "offer"], _REVISION_KEY, "authorization_mismatch")
        clearing = views.clearing_after_cafes
        benefits = _index(clearing["benefits"], _BENEFIT_KEY, "authorization_mismatch")
        for key, action in required_current.items():
            if key not in benefits or benefits[key]["revision"] < action["revision"]:
                raise _BadOutcome("authorization_mismatch", benefit=key, action_id=action["action_id"])
        for key, action in benefits.items():
            if _action(action) != known.get((*key, action["revision"])):
                raise _BadOutcome("authorization_mismatch", benefit=key, action_id=action["action_id"])

        orders = _preserved(expected["clearing_orders"], clearing["orders"], _output_position, "authorization_mismatch")
        revisions = _index(
            clearing["revisions"],
            lambda row: tuple(row["action"][field] for field in _REVISION_KEY),
            "authorization_mismatch",
        )
        for row in [*orders.values(), *(record["action"] for record in revisions.values())]:
            if row["kind"] == "offer":
                continue
            key = (row["carrier"], row["benefit_id"])
            if (
                _action(row) != known.get((*key, row["revision"]))
                or key not in benefits
                or benefits[key]["revision"] < row["revision"]
            ):
                raise _BadOutcome("authorization_mismatch", action_id=row["action_id"], view="clearing delivery")

        aliases = _preserved(expected["vouchers"], clearing["aliases"], ("voucher_id",), reason)
        original_ids = {row["voucher_id"] for row in expected["vouchers"]}
        cutover = clearing.get("issuance_cutover")
        legacy_orders = expected["clearing_orders"]
        if cutover is not None:
            if not _has_fields(cutover, {"orders_through": int}) or not 0 <= cutover["orders_through"] <= len(orders):
                raise _ObservationUnavailable("clearing issuance cutover", response=cutover)
            legacy_orders = clearing["orders"][: cutover["orders_through"]]
        legacy = {
            row["voucher_id"]: row for row in legacy_orders if row["mode"] == "legacy" and row["voucher_id"] is not None
        }
        effective_aliases = defaultdict(set)
        catalogs = {site: _index(view["catalog"], ("voucher_id",), reason) for site, view in views.cafes.items()}
        staged_ids = {site: {row["voucher_id"] for row in view["staged"]} for site, view in views.cafes.items()}
        handouts = {}
        for carrier, view in views.carriers.items():
            actions_by_id = _index(view["actions"], ("action_id",), "authorization_mismatch")
            for handout in view["handouts"]:
                voucher_id = handout["voucher_id"]
                alias = aliases.get((voucher_id,))
                original = actions_by_id.get((handout["action_id"],))
                if alias is None and original is not None:
                    if self._optional_read("clearing", self._key_path("aliases", voucher_id)) is not None:
                        raise _DeliveryPending("carrier handout after clearing snapshot", voucher_id=voucher_id)
                if (
                    alias is None
                    or original is None
                    or any(alias[field] != original[field] for field in (*_REVISION_KEY, "cafe", "amount_cents"))
                    or voucher_id in handouts
                ):
                    raise _BadOutcome(reason, carrier=carrier, voucher_id=voucher_id)
                handouts[voucher_id] = handout

        delivered = set()
        for (voucher_id,), alias in aliases.items():
            key = (alias["carrier"], alias["benefit_id"])
            revision = (*key, alias["revision"])
            action = known.get(revision)
            if (
                action is None
                or action["kind"] not in ("grant", "revise")
                or key not in benefits
                or alias["revision"] > benefits[key]["revision"]
                or any(alias[field] != action[field] for field in ("cafe", "amount_cents"))
            ):
                raise _BadOutcome(reason, voucher_id=voucher_id)
            catalog = catalogs[alias["cafe"]].get((voucher_id,))
            supplied = voucher_id in handouts
            if supplied or catalog is not None or voucher_id in staged_ids[alias["cafe"]] or voucher_id in original_ids:
                effective_aliases[revision].add(voucher_id)
            latest = benefits[key]
            if (supplied or voucher_id in original_ids) and (
                catalog is None or catalog["revision"] < latest["revision"]
            ):
                counter_actions = views.carriers[latest["carrier"]].get("counter_actions", ())
                catalog = self._catalog_after_delivery(clearing, latest, voucher_id, counter_actions=counter_actions)
                if catalog is not None:
                    catalogs[alias["cafe"]][(voucher_id,)] = catalog
            if catalog is None:
                # Replay can allocate a number without supplying it. Later
                # issues can also appear after the café snapshot.
                if supplied or voucher_id in original_ids:
                    raise _BadOutcome(reason, voucher_id=voucher_id, view="missing catalogue entry")
            elif (supplied or voucher_id in original_ids) and catalog["revision"] < latest["revision"]:
                raise _BadOutcome(reason, voucher_id=voucher_id, view="stale catalogue entry")
            elif supplied:
                delivered.add(key)

        for revision, numbers in effective_aliases.items():
            if len(numbers) <= 1:
                continue
            for voucher_id in numbers - original_ids:
                delivery = legacy.get(voucher_id)
                if delivery is None or _action(delivery) != known[revision]:
                    raise _BadOutcome(
                        reason, voucher_id=voucher_id, view="additional effective alias after legacy delivery"
                    )

        for key in required_current:
            if benefits[key]["kind"] != "withdraw" and key not in delivered:
                latest = benefits[key]
                if self._pending_issuer_orders(self._benefit_orders(clearing, latest)):
                    raise _DeliveryPending("carrier handout delivery", action_id=latest["action_id"])
                if any(
                    self._optional_read(key[0], self._key_path("handouts", voucher)) is not None
                    for (voucher,), alias in aliases.items()
                    if (alias["carrier"], alias["benefit_id"]) == key
                ):
                    raise _DeliveryPending("carrier handout after history snapshot", action_id=latest["action_id"])
                raise _BadOutcome(reason, benefit=key, view="missing carrier handout with current catalogue entry")

        for cafe, view in views.cafes.items():
            staged = _index(view["staged"], ("voucher_id",), reason)
            for collection, rows in (("catalog", catalogs[cafe]), ("staged", staged)):
                for (voucher_id,), row in rows.items():
                    alias = aliases.get((voucher_id,))
                    action = known.get(tuple(row[field] for field in _REVISION_KEY))
                    if (
                        alias is None
                        or alias["cafe"] != cafe
                        or action is None
                        or action["cafe"] != cafe
                        or row != _catalog_row(alias, action)
                        or (collection == "catalog" and row["revision"] < alias["revision"])
                    ):
                        raise _BadOutcome(reason, cafe=cafe, voucher_id=voucher_id, view=collection)
        return aliases

    @staticmethod
    def _check_receipts(expected, actions, aliases, views, probe_receipts):
        reason = "receipt_payment_mismatch"
        history_reason = "receipt_history_changed"
        authorized = _index([row for row in actions if row["kind"] != "offer"], _REVISION_KEY, "authorization_mismatch")
        clearing = views.clearing
        uploads = _preserved(expected.get("clearing_uploads", ()), clearing["uploads"], _RECEIPT_KEY, history_reason)
        payments = _preserved(expected["remittances"], clearing["remittances"], _RECEIPT_KEY, reason)
        _preserved(expected["receipt_payments"], payments.values(), _RECEIPT_KEY, reason)
        cursors = _index(clearing["cursors"], ("cafe",), reason)
        accepted = {}
        required_uploads = set()
        for cafe, view in views.cafes.items():
            baseline = expected["receipts"][cafe]
            required = [*baseline, *(row for row in probe_receipts if row["cafe"] == cafe)]
            receipts = _preserved(required, view["receipts"], _RECEIPT_KEY, history_reason)
            _index(view["receipts"], ("request_id",), history_reason)
            sequences = sorted(seq for site, seq in receipts if site == cafe)
            if len(sequences) != len(receipts) or sequences != list(range(1, view["receipt_seq"] + 1)):
                raise _BadOutcome(history_reason, cafe=cafe, view="receipt sequence")
            bound = max((row["receipt_seq"] for row in required), default=0)
            cursor = cursors.get((cafe,), {"receipt_seq": 0})["receipt_seq"]
            if not bound <= cursor <= view["receipt_seq"]:
                raise _BadOutcome(reason, cafe=cafe, cursor=cursor, required_through=bound)
            if sorted(seq for site, seq in payments if site == cafe) != list(range(1, cursor + 1)):
                raise _BadOutcome(reason, cafe=cafe, cursor=cursor, view="payment sequence")
            required_uploads.update((cafe, seq) for seq in range(1, cursor + 1))

            closes = sorted(view["closes"], key=lambda row: row["catalog_revision"])
            _index(closes, ("request_id",), history_reason)
            boundaries = [row["receipt_seq"] for row in closes]
            if (
                boundaries != sorted(boundaries)
                or any(not 0 <= seq <= view["receipt_seq"] for seq in boundaries)
                or view["catalog_revision"] != len(closes)
                or any(
                    (row["expected_catalog_revision"], row["catalog_revision"]) != (i, i + 1)
                    for i, row in enumerate(closes)
                )
                or (closes and view["mode"] != "benefit")
            ):
                raise _BadOutcome(history_reason, cafe=cafe, view="close boundary")
            handoff_sequences = {row["receipt_seq"] for row in baseline}
            spent = set()
            redeemed_aliases = set()
            for seq in sequences:
                key = (cafe, seq)
                receipt = receipts[key]
                benefit = (receipt["carrier"], receipt["benefit_id"])
                voucher_id = receipt["voucher_id"]
                shift = bisect_left(boundaries, seq)
                if (
                    voucher_id in redeemed_aliases
                    or (shift and benefit in spent)
                    or receipt["catalog_revision"] != shift
                ):
                    raise _BadOutcome("voucher_use_incorrect", receipt=key, voucher_id=voucher_id)
                redeemed_aliases.add(voucher_id)
                spent.add(benefit)
                alias = aliases.get((voucher_id,))
                action = authorized.get((*benefit, receipt["revision"]))
                if alias is None or (*benefit, cafe) != (alias["carrier"], alias["benefit_id"], alias["cafe"]):
                    raise _BadOutcome(history_reason, receipt=key, voucher_id=voucher_id)
                if seq not in handoff_sequences and (
                    action is None
                    or action["kind"] == "withdraw"
                    or receipt["allowance_cents"] != action["amount_cents"]
                    or not 0 < receipt["amount_cents"] <= receipt["allowance_cents"]
                ):
                    raise _BadOutcome("voucher_use_incorrect", receipt=key, benefit=benefit)
                accepted[key] = receipt

        if unknown := cursors.keys() - {(cafe,) for cafe in _CAFES}:
            raise _BadOutcome(reason, view="unknown café cursor", cafes=sorted(unknown))
        if missing := required_uploads - uploads.keys():
            raise _BadOutcome(reason, view="missing settled uploads", receipts=sorted(missing))
        for key, row in uploads.items():
            if accepted.get(key) != row:
                raise _BadOutcome(reason, receipt=key, view="uploaded receipt")
        AirportMealClearingOracle._check_settlement(expected, views, accepted, payments)

    @staticmethod
    def _check_settlement(expected, views, accepted, payments):
        reason = "receipt_payment_mismatch"
        posted = {
            (carrier, row["support_id"]): {**row, "carrier": carrier}
            for carrier, view in views.carriers.items()
            for row in view["support"]
            if row["status"] == "posted"
        }
        clearing = views.clearing
        mirrored = _index(clearing["support"], ("carrier", "support_id"), reason)
        mandatory = {
            (carrier, row["support_id"])
            for carrier, baseline in expected["carriers"].items()
            for row in baseline["support"]
        }
        if missing := mandatory - mirrored.keys():
            raise _BadOutcome(reason, view="missing required clearing support", support=sorted(missing))
        for key, entry in mirrored.items():
            if posted.get(key) != entry:
                raise _BadOutcome(reason, support=key, view="clearing support differs from carrier posting")

        events = clearing["settlement_events"]
        prefix = expected.get("settlement_events", [])
        if events[: len(prefix)] != prefix:
            raise _BadOutcome("receipt_history_changed", view="original clearing settlement event prefix")
        visible_support = defaultdict(list)
        remaining_support = set(mirrored)
        remaining_payments = set(payments)
        cursor = Counter()
        spent = set()
        for sequence, event in enumerate(events, 1):
            if event["sequence"] != sequence:
                raise _BadOutcome(reason, sequence=sequence, view="settlement event order")
            if event["kind"] == "support":
                key = (event["carrier"], event["support_id"])
                if key not in remaining_support:
                    raise _BadOutcome(reason, sequence=sequence, support=key)
                remaining_support.remove(key)
                entry = posted[key]
                if release := entry.get("release_payment"):
                    release_key = (release["cafe"], release["receipt_seq"])
                    if payments.get(release_key) != release or release_key in remaining_payments:
                        raise _BadOutcome(reason, sequence=sequence, support=key, view="support release payment")
                visible_support[(entry["carrier"], entry["benefit_id"])].append(entry)
                continue
            if event["kind"] != "payment":
                raise _BadOutcome(reason, sequence=sequence, view="unknown settlement event kind")
            cafe = event["cafe"]
            key = (cafe, event["receipt_seq"])
            cursor[cafe] += 1
            if key not in remaining_payments or key not in accepted or key[1] != cursor[cafe]:
                raise _BadOutcome(reason, sequence=sequence, receipt=key, view="payment event order")
            remaining_payments.remove(key)
            receipt = accepted[key]
            benefit = (receipt["carrier"], receipt["benefit_id"])
            covered = 0
            if benefit not in spent and receipt["allowance_cents"] > 0:
                covered = min(receipt["allowance_cents"], receipt["amount_cents"])
                spent.add(benefit)
            support_cents = 0
            support_ids = []
            for entry in visible_support[benefit]:
                if support_cents == covered:
                    break
                support_cents += min(entry["amount_cents"], covered - support_cents)
                support_ids.append(entry["support_id"])
            wanted = {
                **{field: receipt[field] for field in (*_RECEIPT_KEY, *_BENEFIT_KEY, "amount_cents")},
                "carrier_cents": covered - support_cents,
                "support_cents": support_cents,
                "operator_cents": receipt["amount_cents"] - covered,
                "support_ids": support_ids,
            }
            if payments[key] != wanted:
                raise _BadOutcome(reason, receipt=key, sequence=sequence, expected=wanted, actual=payments[key])

        if remaining_support or remaining_payments:
            raise _BadOutcome(
                reason,
                view="missing settlement events",
                support=sorted(remaining_support),
                receipts=sorted(remaining_payments),
            )

    @staticmethod
    def _check_aborted(action_ids, views):
        forbidden = set(action_ids)
        for collection in ("orders", "benefits", "offers"):
            for row in views.clearing_after_cafes[collection]:
                if row["action_id"] in forbidden:
                    raise _BadOutcome("aborted_input_visible", action_id=row["action_id"], view=collection)

    @staticmethod
    def _key_path(collection, *key):
        return "/" + "/".join((collection, *(quote(str(part), safe="") for part in key)))

    @staticmethod
    def _benefit_orders(clearing, action):
        return [
            row
            for row in clearing["orders"]
            if row["kind"] != "offer" and all(row[field] == action[field] for field in _BENEFIT_KEY)
        ]

    def _pending_issuer_orders(self, orders):
        positions = {_output_position(row) for row in orders if row["output_topic"] == ORDERS_TOPIC}
        if not positions:
            return []
        stream = self.problem.stream
        try:
            topic_id = self._stream_call(stream.topic_id, ORDERS_TOPIC, timeout=8)
            # The issuer commits after the clearing write, café update and handout.
            offsets = self._stream_call(stream.group_offsets, ISSUER_GROUP, topic=ORDERS_TOPIC, timeout=8)
        except _ObservationUnavailable as exc:
            raise _DeliveryPending("issuer progress read", error=exc.detail) from exc
        except (AttributeError, TypeError, ValueError) as exc:
            raise _ObservationUnavailable("issuer progress read", error=str(exc)) from exc
        if not isinstance(topic_id, str) or not topic_id:
            raise _ObservationUnavailable("issuer output topic ID", response=topic_id)
        if any(topic not in (topic_id, ORDERS_TOPIC) for topic, _, _ in positions):
            raise _ObservationUnavailable("issuer output topic incarnation", topic_id=topic_id)
        if not isinstance(offsets, dict) or any(type(value) is not int or value < 0 for value in offsets.values()):
            raise _ObservationUnavailable("issuer group positions", response=offsets)
        return sorted((topic, part, offset) for topic, part, offset in positions if offsets.get(part, -1) <= offset)

    def _catalog_after_delivery(self, clearing, current, voucher, *, counter_actions=()):
        action = _action(current)
        action_id = action["action_id"]
        orders = self._benefit_orders(clearing, current)
        record = None
        if not any(row["action_id"] == action_id and row["output_topic"] == ORDERS_TOPIC for row in orders):
            carrier = current["carrier"]
            path = self._key_path("counter-actions", action_id)
            record = next(
                (row for row in counter_actions if _valid_read_payload(carrier, path, row) and row["action"] == action),
                None,
            )
            if record is None:
                record = self._optional_read(carrier, path)

        pending = []
        if record is None:
            pending = self._pending_issuer_orders(orders)
        else:
            # Completion is written after the café update. Read it before the
            # refreshed café row so completion cannot make an older row fail.
            completion = self._optional_read(carrier, self._key_path("counter-dispositions", action_id))

        catalog = self._optional_read(current["cafe"], self._key_path("catalog", voucher))
        benefit_path = self._key_path("benefits", *(current[field] for field in _BENEFIT_KEY))
        if current != self._optional_read("clearing", benefit_path):
            raise _DeliveryPending("clearing authorization changed during café read", action_id=action_id)

        wanted = _catalog_row(current | {"voucher_id": voucher}, current)
        if record is not None:
            reason = "counter_withdrawal_mismatch"
            if record["action"] != action:
                raise _BadOutcome(reason, action_id=action_id, view="current counter action")
            if completion is None:
                if catalog != wanted:
                    raise _DeliveryPending("counter café delivery", action_id=action_id)
            elif completion != {
                "action_id": action_id,
                "record_id": record["record_id"],
                "disposition": "fulfilled-locally",
            }:
                raise _BadOutcome(reason, action_id=action_id, view="current counter completion")
        if pending and catalog != wanted:
            raise _DeliveryPending("issuer café delivery", action_id=action_id, pending=pending)
        return catalog

    def _counter_catalog(self, action, voucher):
        reason = "counter_withdrawal_mismatch"
        key = tuple(action[field] for field in _BENEFIT_KEY)
        benefit_path = self._key_path("benefits", *key)
        deadline = self._deadline(30)
        while True:
            current = self._read("clearing", benefit_path, reason=reason)
            authority = self._read(action["carrier"], self._key_path("actions", current["action_id"]), reason=reason)
            if (
                any(current[field] != action[field] for field in (*_BENEFIT_KEY, "cafe"))
                or current["revision"] < action["revision"]
                or _action(current) != _action(authority)
            ):
                raise _BadOutcome(reason, action_id=action["action_id"], view="current counter authorization")
            catalog = self._optional_read(action["cafe"], self._key_path("catalog", voucher))
            if catalog != _catalog_row(action | {"voucher_id": voucher}, current):
                clearing = self._read("clearing", "/state")
                observed = next(
                    (row for row in clearing["benefits"] if tuple(row[f] for f in _BENEFIT_KEY) == key), None
                )
                try:
                    if observed != current:
                        raise _DeliveryPending("clearing authorization changed during counter read", benefit=key)
                    catalog = self._catalog_after_delivery(clearing, current, voucher)
                except _DeliveryPending as exc:
                    self._wait_for_partner(deadline, exc)
                    continue
            if catalog != _catalog_row(action | {"voucher_id": voucher}, current):
                raise _BadOutcome(reason, action_id=action["action_id"], view="current counter catalog")
            if current == self._read("clearing", benefit_path, reason=reason):
                return catalog
            self._wait_for_partner(deadline, _DeliveryPending("stable counter authorization", benefit=key))

    def _write_with_readback(self, site, path, request, key_path=None):
        try:
            return self._request(site, path, request)
        except _ObservationUnavailable:
            if key_path is not None:
                row = self._optional_read(site, key_path)
                if row is not None:
                    return 200, row
            raise

    def _post(self, site, path, request, reason, *, key_path=None):
        status, row = self._write_with_readback(site, path, request, key_path)
        if status == 400:
            raise _ProbeError(f"POST {site} {path} rejected the probe input: {row}")
        if status != 200:
            raise _BadOutcome(reason, site=site, path=path, status=status, response=row)
        if not isinstance(row, dict):
            raise _ProbeError(f"{site} {path} returned an invalid acknowledgment")
        return row

    def _retry_carrier_records(self, expected, precheck):
        for carrier, baseline in expected["carriers"].items():
            view = precheck.carriers[carrier]
            support = {row["support_id"]: row for row in view["support"]}
            for request in baseline["support_requests"]:
                support_id = request["support_id"]
                reason = "carrier_history_changed"
                if (
                    self._read(carrier, f"/support-requests/{quote(support_id, safe='')}", reason=reason) != request
                    or self._post(carrier, "/support", request, reason, key_path=self._key_path("support", support_id))
                    != support[support_id]
                ):
                    raise _BadOutcome(reason, carrier=carrier, support_id=support_id)

            amendments = {row["request_id"]: row for row in baseline["amendments"] + view["amendments"][-1:]}
            for amendment in amendments.values():
                if amendment["request_id"].startswith("receiver:"):
                    continue
                reason = "carrier_statement_mismatch"
                account_path = f"/accounts/{quote(amendment['statement_id'], safe='')}"
                account = self._read(carrier, account_path, reason=reason)
                request = {k: v for k, v in amendment.items() if k != "account_revision"}
                actual = self._post(
                    carrier,
                    "/amendments",
                    request,
                    reason,
                    key_path=self._key_path("amendments", amendment["request_id"]),
                )
                after = self._read(carrier, account_path, reason=reason)
                if (
                    actual != amendment
                    or self._read(carrier, f"/amendments/{quote(amendment['request_id'], safe='')}", reason=reason)
                    != amendment
                    or after["account_revision"] < account["account_revision"]
                    or (after["account_revision"] == account["account_revision"] and after != account)
                ):
                    raise _BadOutcome(reason, carrier=carrier, amendment=amendment["request_id"])

    @staticmethod
    def _alias_pairs(views):
        # Choose independent historical pairs so that both printed-number
        # orders can be exercised while leaving other pairs untouched.
        spent = {(row["carrier"], row["benefit_id"]) for view in views.cafes.values() for row in view["receipts"]}
        held = defaultdict(list)
        supplied = {row["voucher_id"] for view in views.carriers.values() for row in view["handouts"]}
        for alias in views.clearing_after_cafes["aliases"]:
            if alias["voucher_id"] in supplied:
                held[(alias["carrier"], alias["benefit_id"])].append(alias)
        choices = []
        for benefit, aliases in sorted(held.items()):
            if len(aliases) < 2 or benefit in spent:
                continue
            cafe = aliases[0]["cafe"]
            catalog = {row["voucher_id"]: row for row in views.cafes[cafe]["catalog"]}
            valid = sorted(
                row["voucher_id"]
                for row in aliases
                if (entry := catalog.get(row["voucher_id"])) and entry["active"] and entry["amount_cents"] > 0
            )
            if len(valid) >= 2:
                choices.append((cafe, catalog[valid[0]], valid[1]))
        return choices[:2]

    def _redeem(self, cafe, catalog, request_id, *, allow_later_revision=False):
        reason = "voucher_use_incorrect"
        sale = {
            "request_id": request_id,
            "voucher_id": catalog["voucher_id"],
            "amount_cents": catalog["amount_cents"],
        }
        receipt = self._post(cafe, "/receipts", sale, reason, key_path=self._key_path("requests", request_id))
        expected = {
            **sale,
            "cafe": cafe,
            "carrier": catalog["carrier"],
            "benefit_id": catalog["benefit_id"],
        }
        same_authorization = (receipt["revision"], receipt["allowance_cents"]) == (
            catalog["revision"],
            catalog["amount_cents"],
        )
        later_authorization = (
            allow_later_revision
            and receipt["revision"] > catalog["revision"]
            and receipt["allowance_cents"] >= sale["amount_cents"]
        )
        if any(receipt.get(field) != value for field, value in expected.items()) or not (
            same_authorization or later_authorization
        ):
            raise _BadOutcome(reason, cafe=cafe, voucher_id=sale["voucher_id"])
        return sale, receipt

    def _redeem_existing(self, cafe, catalog, request_id):
        voucher_id = catalog["voucher_id"]
        path = self._key_path("catalog", voucher_id)
        current = self._read(cafe, path, reason="voucher_use_incorrect")
        if any(current[field] != catalog[field] for field in ("voucher_id", *_BENEFIT_KEY)):
            raise _BadOutcome("voucher_use_incorrect", cafe=cafe, voucher_id=voucher_id, view="catalog identity")
        if not current["active"] or current["amount_cents"] == 0:
            return None
        try:
            return self._redeem(cafe, current, request_id, allow_later_revision=True)
        except _BadOutcome as exc:
            if exc.detail.get("status") != 409:
                raise
            error = exc.detail["response"]["error"]
            view = self._read(cafe, "/state")
            key = (catalog["carrier"], catalog["benefit_id"])
            if error in ("benefit already used", "voucher already used") and any(
                (row["carrier"], row["benefit_id"]) == key
                and (error == "benefit already used" or row["voucher_id"] == voucher_id)
                for row in view["receipts"]
            ):
                return None
            if error in ("voucher is inactive", "sale exceeds the allowance") and (
                self._read(cafe, path, reason="voucher_use_incorrect")["revision"] > current["revision"]
            ):
                return None
            raise

    def _must_refuse(self, cafe, voucher_id, request_id, *, withdrawn=False, spent_voucher=None):
        reason = "voucher_use_incorrect"
        path = self._key_path("catalog", voucher_id)
        catalog = self._read(cafe, path, reason=reason)
        if catalog["active"] == withdrawn or (not withdrawn and catalog["amount_cents"] == 0):
            return False
        expected_errors = {"voucher is inactive"} if withdrawn else {"benefit already used"}
        if not withdrawn and spent_voucher == voucher_id:
            expected_errors.add("voucher already used")
        request_path = self._key_path("requests", request_id)
        status, row = self._write_with_readback(
            cafe, "/receipts", {"request_id": request_id, "voucher_id": voucher_id, "amount_cents": 1}, request_path
        )
        if status == 400:
            raise _ProbeError(f"{cafe} rejected the refusal probe input: {row}")
        if status == 200 and withdrawn and row["revision"] > catalog["revision"]:
            current = self._read(cafe, path, reason=reason)
            if current["active"] and current["revision"] == row["revision"]:
                self._remit(row)
                return False
        if status != 409:
            raise _BadOutcome(reason, cafe=cafe, voucher_id=voucher_id, status=status, response=row)
        if self._optional_read(cafe, request_path) is not None:
            raise _BadOutcome(reason, cafe=cafe, voucher_id=voucher_id, response=row)
        if row["error"] in expected_errors:
            if withdrawn:
                logger.info(
                    "Airport meal withdrawn sale refused cafe=%s request=%s voucher=%s", cafe, request_id, voucher_id
                )
            return True
        if row["error"] in ("voucher is inactive", "sale exceeds the allowance") and not withdrawn:
            latest = self._read(cafe, path, reason=reason)
            if latest["revision"] > catalog["revision"]:
                return False
        raise _BadOutcome(reason, cafe=cafe, voucher_id=voucher_id, response=row)

    def _exercise_carried_spends(self, views, run_id):
        for cafe, view in views.cafes.items():
            spent = {(row["carrier"], row["benefit_id"]) for row in view["receipts"]}
            used_numbers = {row["voucher_id"] for row in view["receipts"]}
            for row in view["catalog"]:
                voucher_id = row["voucher_id"]
                if (
                    row["active"]
                    and row["amount_cents"] > 0
                    and voucher_id not in used_numbers
                    and (row["carrier"], row["benefit_id"]) in spent
                ):
                    self._must_refuse(cafe, voucher_id, f"{run_id}-{cafe}-{voucher_id}-old-spend")

    def _remit(self, receipt):
        reason = "receipt_payment_mismatch"
        cafe = receipt["cafe"]
        seq = receipt["receipt_seq"]
        cursor = self._read("clearing", f"/cursors/{cafe}", reason=reason)["receipt_seq"]
        # A concurrent sale can precede the probe, even if its upload is
        # still pending. Pay through the probe's sequence.
        for position in range(min(cursor + 1, seq), seq + 1):
            accepted = receipt if position == seq else self._read(cafe, f"/receipts/{position}", reason=reason)
            if (
                self._post("clearing", "/uploads", accepted, reason, key_path=self._key_path("uploads", cafe, position))
                != accepted
            ):
                raise _BadOutcome(reason, cafe=cafe, receipt_seq=position)
        payment_path = self._key_path("remittances", cafe, seq)
        row = self._optional_read("clearing", payment_path)
        if row is not None:
            return row
        try:
            self._post("clearing", "/remittances", {"cafe": cafe, "through_receipt_seq": seq}, reason)
        except _ObservationUnavailable:
            row = self._optional_read("clearing", payment_path)
            if row is not None:
                return row
            raise
        return self._read("clearing", payment_path, reason=reason)

    def _exercise_supported_purchase(self, views, run_id):
        supported = {(row["carrier"], row["benefit_id"]) for row in views.clearing["support"]}
        spent = {(row["carrier"], row["benefit_id"]) for view in views.cafes.values() for row in view["receipts"]}
        for cafe, view in views.cafes.items():
            for row in view["catalog"]:
                key = (row["carrier"], row["benefit_id"])
                if key in spent or not row["active"] or not row["amount_cents"] or key not in supported:
                    continue
                if sale := self._redeem_existing(cafe, row, f"{run_id}-{row['voucher_id']}-supported-sale"):
                    _, receipt = sale
                    return receipt, self._remit(receipt)
        return None

    def _restart_partners(self, sites, *, keyed_reads=()):
        namespace = self.problem.namespace
        kubectl = self.problem.kubectl
        deadline = self._deadline(self.evaluation_timeout_seconds)
        # Resolve serving Deployments from each Service so an in-place
        # replacement of a Deployment is exercised the same way.
        controllers = {
            "deployment": self._stream_call(kubectl.list_deployments, namespace, timeout=10).items,
            "statefulset": self._stream_call(
                kubectl.apps_v1_api.list_namespaced_stateful_set, namespace, _request_timeout=10
            ).items,
        }
        targets = {}
        for site in sites:
            service = self._stream_call(
                kubectl.core_v1_api.read_namespaced_service, site, namespace, _request_timeout=10
            )
            selector = service.spec.selector
            matches = {
                (kind, workload.metadata.name): workload.metadata.generation
                for kind, workloads in controllers.items()
                for workload in workloads
                if selector
                and selector.items() <= (workload.spec.template.metadata.labels or {}).items()
                and workload.spec.replicas != 0
            }
            if not matches:
                raise _ObservationUnavailable(f"Could not resolve an ordinary restart for {site}")
            targets.update(matches)
        dispatchers = {
            (kind, workload.metadata.name): workload.metadata.generation
            for kind, workloads in controllers.items()
            for workload in workloads
            if workload.spec.replicas != 0
            and any(
                [*(container.command or ()), *(container.args or ())][-2:] == ["airport.meal.MealBridge", "dispatch"]
                for container in workload.spec.template.spec.containers
            )
        }
        if len(dispatchers) != 1:
            raise _ObservationUnavailable("resolve desk dispatcher workload", workloads=sorted(dispatchers))
        targets.update(dispatchers)
        readers = {
            "deployment": kubectl.apps_v1_api.read_namespaced_deployment,
            "statefulset": kubectl.apps_v1_api.read_namespaced_stateful_set,
        }

        def request_restart(target):
            command = ["kubectl", "-n", namespace, "rollout", "restart", "/".join(target)]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"error": "evaluation budget exhausted"}
            try:
                self._stream_call(kubectl.exec_command_checked, shlex.join(command), timeout=min(30, remaining))
            except _ObservationUnavailable as exc:
                return exc.detail
            return None

        def readiness(target):
            kind, name = target
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"error": "evaluation budget exhausted"}
            try:
                workload = self._stream_call(readers[kind], name, namespace, _request_timeout=min(5, remaining))
            except _ObservationUnavailable as exc:
                return exc.detail
            generation = workload.metadata.generation
            status = workload.status
            replicas = workload.spec.replicas
            if (
                generation is not None
                and targets[target] is not None
                and generation > targets[target]
                and (status.observed_generation or 0) >= generation
                and status.replicas == status.updated_replicas == status.ready_replicas == replicas
                and (
                    status.available_replicas == replicas
                    if kind == "deployment"
                    else status.current_revision is not None and status.current_revision == status.update_revision
                )
            ):
                return None
            return {"generation": generation, "observed": status.observed_generation, "ready": status.ready_replicas}

        def observe_key(key):
            site, path = key
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "evaluation budget exhausted"
            result = self._read_result(site, path, timeout=min(10, remaining))
            if path != "/health":
                # Confirmed absence is a usable read. The subsequent business
                # checks validate exact records and grade any missing history.
                return None if result.state in (_ReadState.FOUND, _ReadState.MISSING) else result.state.name
            if result.state is _ReadState.FOUND:
                row = result.payload
                if row["ok"] and (row["role"], row["site"]) == (_partner_role(site), site):
                    return None
            return result.state.name, result.payload

        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            names = sorted(targets)
            issued = {
                "/".join(name): error
                for name, error in zip(names, pool.map(request_restart, names), strict=True)
                if error
            }
            keys = sorted({(site, "/health") for site in sites} | set(keyed_reads))
            while True:
                workloads = {
                    "/".join(name): err for name, err in zip(names, pool.map(readiness, names), strict=True) if err
                }
                health = {
                    f"{site} {path}": err
                    for (site, path), err in zip(keys, pool.map(observe_key, keys), strict=True)
                    if err
                }
                if not workloads and not health:
                    logger.info("Airport meal partner rollouts ready %s", ["/".join(name) for name in names])
                    return
                self._wait_for_partner(
                    deadline,
                    _ObservationUnavailable(
                        "partner restart readiness", workloads=workloads, health=health, issued=issued
                    ),
                )

    @staticmethod
    def _probe_actions(run_id, wave, *, rotation=0, kind="grant"):
        actions = []
        for partition in _PARTITIONS:
            carrier = _CARRIERS[(partition + rotation) % len(_CARRIERS)]
            prefix = f"{run_id}-{wave}-{partition}"
            candidate = 0
            while _desk_partition(carrier, f"{prefix}-{candidate}") != partition:
                candidate += 1
            identity = f"{prefix}-{candidate}"
            actions.append(
                {
                    "action_id": identity,
                    "benefit_id": identity,
                    "carrier": carrier,
                    "cafe": _CAFES[(partition + rotation) % len(_CAFES)],
                    "revision": 1,
                    "amount_cents": 900 + partition * 19,
                    "kind": kind,
                    "bank": "live-shift",
                }
            )
        return actions

    def _submit(self, actions):
        for action in actions:
            actual = self._post(
                action["carrier"],
                "/actions",
                action,
                "live_issue_mismatch",
                key_path=self._key_path("actions", action["action_id"]),
            )
            if _action(actual) != action:
                raise _BadOutcome("live_issue_mismatch", action_id=action["action_id"], view="carrier intake")

    def _await_dispatches(self, actions, *, timeout=60):
        deadline = time.monotonic() + timeout
        while True:
            wanted = {(row["carrier"], row["action_id"]) for row in actions}
            observed = {
                (carrier, row["action_id"]): row
                for carrier in {action["carrier"] for action in actions}
                for row in self._read(carrier, "/state")["dispatches"]
            }
            missing = wanted - observed.keys()
            if not missing:
                return {
                    action_id: self._read(
                        carrier, self._key_path("dispatches", action_id), reason="live_issue_mismatch"
                    )
                    for carrier, action_id in wanted
                }
            if time.monotonic() >= deadline:
                raise _ObservationUnavailable("desk dispatch progress", missing=sorted(missing))
            time.sleep(1)

    def _check_new_issues(self, actions, *, timeout=60):
        reason = "live_issue_mismatch"
        required = _current(actions)
        deadline = self._deadline(timeout)
        while True:
            views = self._read_views()
            clearing = views.clearing_after_cafes
            benefits = _index(clearing["benefits"], _BENEFIT_KEY, reason)
            authority = {row["action_id"]: _action(row) for view in views.carriers.values() for row in view["actions"]}
            aliases = defaultdict(list)
            for row in clearing["aliases"]:
                key = (row["carrier"], row["benefit_id"])
                if key in required:
                    aliases[key].append(row)
            handouts = {
                row["voucher_id"]: row["action_id"] for view in views.carriers.values() for row in view["handouts"]
            }
            reports = {}
            for key, action in required.items():
                latest = benefits.get(key)
                if latest is None or latest["revision"] < action["revision"]:
                    reports[key] = "clearing has not applied the action"
                    continue
                if _action(latest) != authority.get(latest["action_id"]) or (
                    latest["revision"] == action["revision"] and _action(latest) != action
                ):
                    raise _BadOutcome(reason, action_id=action["action_id"], view="clearing authorization")
                cafe = latest["cafe"]
                cafe_view = views.cafes[cafe]
                catalog_rows = cafe_view["catalog"]
                positions = {row["voucher_id"]: i for i, row in enumerate(catalog_rows)}
                staged = {row["voucher_id"] for row in cafe_view["staged"]}
                effective = Counter()
                supplied = False
                for alias in aliases[key]:
                    voucher_id = alias["voucher_id"]
                    position = positions.get(voucher_id)
                    catalog = None if position is None else catalog_rows[position]
                    if voucher_id in handouts and catalog != _catalog_row(alias, latest):
                        counter_actions = views.carriers[latest["carrier"]].get("counter_actions", ())
                        try:
                            catalog = self._catalog_after_delivery(
                                clearing, latest, voucher_id, counter_actions=counter_actions
                            )
                        except _DeliveryPending:
                            reports[key] = "current catalog delivery"
                            continue
                    if catalog is not None or voucher_id in handouts or voucher_id in staged:
                        effective[alias["revision"]] += 1
                    if voucher_id not in handouts:
                        continue
                    issued = authority.get(handouts[voucher_id])
                    if issued is None or any(
                        alias[field] != issued[field] for field in (*_REVISION_KEY, "cafe", "amount_cents")
                    ):
                        raise _BadOutcome(reason, voucher_id=voucher_id, view="handout authorization")
                    supplied = True
                    if catalog != _catalog_row(alias, latest):
                        raise _BadOutcome(reason, voucher_id=voucher_id, view="current catalog delivery")
                    if position is None:
                        catalog_rows.append(catalog)
                    else:
                        catalog_rows[position] = catalog
                if any(count > 1 for count in effective.values()):
                    raise _BadOutcome(reason, benefit=key, view="additional printable or redeemable number")
                if not supplied and key not in reports:
                    if self._pending_issuer_orders(self._benefit_orders(clearing, latest)):
                        reports[key] = "carrier handout"
                        continue
                    # Carrier snapshots can precede the final handout even if
                    # the offset was committed just after the read.
                    handout = any(
                        self._optional_read(key[0], self._key_path("handouts", alias["voucher_id"])) is not None
                        for alias in aliases[key]
                    )
                    if not handout:
                        raise _BadOutcome(reason, benefit=key, view="missing carrier handout")
                    reports[key] = "carrier handout"
            if not reports:
                return views
            self._wait_for_partner(
                deadline, _ObservationUnavailable("current voucher delivery", missing=sorted(reports.items()))
            )

    @staticmethod
    def _input_position(row):
        return row["input_topic"], row["input_partition"], row["input_offset"]

    @staticmethod
    def _broker_position(row):
        return row["topic"], row["partition"], row["offset"]

    @staticmethod
    def _check_dispatch_coverage(actions, dispatches, input_id):
        for action in actions:
            ack = dispatches[action["action_id"]]
            partition = _desk_partition(action["carrier"], action["benefit_id"])
            if (ack["action_id"], ack["input_topic"], ack.get("input_topic_id"), ack["input_partition"]) != (
                action["action_id"],
                "allowances",
                input_id,
                partition,
            ):
                raise _BadOutcome(
                    "live_issue_mismatch",
                    action_id=action["action_id"],
                    view="carrier dispatch coordinate",
                    expected_partition=partition,
                    dispatch=ack,
                )

    @staticmethod
    def _stream_call(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (ApiException, HTTPError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
            raise _ObservationUnavailable(f"Flink/Kafka {operation.__name__}", error=str(exc)) from exc

    def _production_release(self, job_id=None):
        reason = "live_issue_mismatch"
        stream = self.problem.stream
        try:
            release = self._stream_call(stream.release)
        except (KeyError, TypeError, ValueError) as exc:
            raise _ObservationUnavailable("Flink production release", error=str(exc)) from exc
        if release is None:
            raise _BadOutcome(reason, view="missing production release")
        try:
            if not release["job_id"]:
                raise _BadOutcome(reason, view="missing production release")
            if job_id is not None and release["job_id"] != job_id:
                raise _BadOutcome(reason, view="changed production release", job_id=job_id, release=release["job_id"])
            validate_release_artifact(release)
            if release["route"] != "production" or release["target_topic"] != "issue-orders":
                raise _BadOutcome(
                    reason, view="production route", route=release["route"], topic=release["target_topic"]
                )
            release_status = release["status"]
            reader_name = f"Source: {release['reader']}"
            size = release["parallelism"]
        except (ValueError, KeyError, TypeError) as exc:
            raise _BadOutcome(reason, view="invalid production release", error=str(exc)) from exc
        try:
            job = self._stream_call(stream.live_job, release)
        except (KeyError, TypeError, ValueError) as exc:
            raise _ObservationUnavailable("Flink live_job", job_id=release["job_id"], error=str(exc)) from exc
        if job is None:
            raise _BadOutcome(reason, job_id=release["job_id"], view="missing live production job")
        state = job["state"]
        if state != "RUNNING" or release_status != "RUNNING":
            raise _BadOutcome(reason, job_id=release["job_id"], status=state, release_status=release_status)
        sources = [v for v in job["vertices"] if v["name"] == reader_name or v["name"].startswith(reader_name + " -> ")]
        if len(sources) != 1 or sources[0]["parallelism"] != size:
            raise _BadOutcome(reason, job_id=release["job_id"], view="registered source and parallelism")
        return release

    def _require_reader_ownership(self, stream, job_id, *, timeout=30):
        reason = "live_issue_mismatch"
        deadline = time.monotonic() + timeout
        while True:
            observation = self._stream_call(stream.assignment_observation, job_id)
            if type(observation["complete"]) is not bool or not isinstance(observation["owners"], dict):
                raise _ProbeError("Invalid native reader observation")
            if observation["complete"] and (
                set(observation["owners"]) != set(_PARTITIONS)
                or any(not isinstance(rows, list) for rows in observation["owners"].values())
            ):
                raise _ProbeError("Complete reader observation must include assignments for all partitions")
            counts = {partition: len(rows) for partition, rows in observation["owners"].items()}
            if observation["complete"] and counts == dict.fromkeys(_PARTITIONS, 1):
                self._production_release(job_id)
                return
            if time.monotonic() >= deadline:
                if not observation["complete"]:
                    raise _ObservationUnavailable(
                        "native partition ownership",
                        job_id=job_id,
                        observation_reason=observation.get("reason"),
                        owners=counts,
                    )
                raise _BadOutcome(reason, job_id=job_id, view="native partition ownership", owners=counts)
            time.sleep(1)

    def _resize(self, stream, release, while_stopped):
        old_job = release["job_id"]
        size = release["parallelism"]
        # A downward resize fits the existing session. A single-reader job
        # needs one spare slot to exercise a different parallelism.
        if size == 1 and self._stream_call(stream.available_slots, release) < 1:
            raise _BadOutcome("live_issue_mismatch", job_id=old_job, view="no spare slot for resize")
        parallelism = 2 if size == 1 else min(3, size - 1)
        savepoint = self._stream_call(stream.stop, old_job, drain=False)
        if not savepoint:
            raise _ProbeError("Flink stop did not return a canonical savepoint")
        try:
            old_end = self._output_ends(stream, release["target_topic"])
            stopped_output_end = while_stopped()
        finally:
            new_job = self._stream_call(stream.restore_in_place, release, savepoint, parallelism=parallelism)
        if new_job == old_job:
            raise _ProbeError("Flink returned the same job ID after restore")
        restored = self._production_release(new_job)
        changed = {
            key: value
            for key, value in release.items()
            if key not in {"job_id", "reader", "status", "parallelism", "savepoint", "stopped_savepoint"}
            and restored.get(key) != value
        }
        if changed or restored["parallelism"] != parallelism:
            raise _BadOutcome("live_issue_mismatch", job_id=new_job, view="restore settings", changed=sorted(changed))
        self._require_reader_ownership(stream, new_job)
        return restored, old_end, stopped_output_end

    def _output_ends(self, stream, topic):
        return {p: row["end"] for p, row in self._stream_call(stream.bounds, topic).items()}

    def _output_interval(self, topic, topic_id, start, end):
        stream = self.problem.stream
        if start.keys() != end.keys() or any(end[p] < offset for p, offset in start.items()):
            raise _ObservationUnavailable("stable production output range", start=start, end=end)
        if self._stream_call(stream.topic_id, topic) != topic_id:
            raise _BadOutcome("live_issue_mismatch", view="production topic identity changed", topic=topic)
        rows = self._stream_call(stream.read_records, topic, offsets=start) if start != end else []
        if self._stream_call(stream.topic_id, topic) != topic_id:
            raise _ObservationUnavailable("production topic changed during broker read", topic=topic)
        return [
            (self._broker_position(row), json.loads(row["value"]))
            for row in rows
            if start[row["partition"]] <= row["offset"] < end[row["partition"]]
        ]

    def _check_orders(self, orders, writer, topic_id, input_id, actions, dispatches, *, replays=None, aborted_ids=()):
        reason = "live_issue_mismatch"
        by_id = {row["action_id"]: row for row in actions}
        replay_positions = replays or {}
        wanted = {action_id: self._input_position(row) for action_id, row in dispatches.items()}
        seen = set()
        deliveries = []
        for (topic, partition, offset), wire in orders:
            observed_writer = wire.get("writer_job_id")
            if observed_writer is None:
                raise _ObservationUnavailable(
                    "Flink writer provenance", topic_id=topic_id, position=(partition, offset)
                )
            if not isinstance(observed_writer, str) or not observed_writer:
                raise _ProbeError("Invalid writer_job_id in broker output")
            if observed_writer != writer:
                raise _BadOutcome(
                    reason,
                    view="competing production writer",
                    job_id=writer,
                    observed=observed_writer,
                    topic_id=topic_id,
                    partition=partition,
                    offset=offset,
                )
            action_id = wire["action"]["action_id"]
            if action_id in aborted_ids:
                raise _BadOutcome("aborted_input_visible", action_id=action_id, view="Flink output")
            if action_id not in by_id:
                continue
            position = self._input_position(wire)
            if _action(wire["action"]) != by_id[action_id] or (
                position != wanted.get(action_id) and position not in replay_positions.get(action_id, ())
            ):
                raise _BadOutcome(reason, action_id=action_id, coordinate=position)
            if position == wanted.get(action_id):
                seen.add(action_id)
            delivery = {
                **by_id[action_id],
                "writer_job_id": writer,
                "input_topic": position[0],
                "input_topic_id": input_id,
                "input_partition": position[1],
                "input_offset": position[2],
                "output_topic": topic,
                "output_topic_id": topic_id,
                "output_partition": partition,
                "output_offset": offset,
            }
            deliveries.append((delivery, position == wanted.get(action_id)))
        return deliveries, set(wanted) - seen

    def _await_order_deliveries(self, deliveries, *, timeout=60):
        reason = "live_issue_mismatch"
        deadline = self._deadline(timeout)
        orders = [row for row, _ in deliveries]
        while True:
            try:
                pending = self._pending_issuer_orders(orders)
            except _DeliveryPending as exc:
                error = exc
            else:
                if not pending:
                    break
                error = _ObservationUnavailable("issuer delivery completion", pending=pending)
            self._wait_for_partner(deadline, error)
        candidates = defaultdict(set)
        for wanted, original in deliveries:
            position = _output_position(wanted)
            delivered = self._read("clearing", self._key_path("orders", *position), reason=reason)
            if any(delivered.get(field) != value for field, value in wanted.items()):
                raise _BadOutcome(
                    reason, action_id=wanted["action_id"], view="keyed clearing delivery", coordinate=position
                )
            if original and wanted["kind"] == "grant":
                numbers = candidates[wanted["action_id"]]
                if delivered["voucher_id"] is not None:
                    numbers.add((wanted["carrier"], delivered["voucher_id"]))
        for action_id, numbers in candidates.items():
            supplied = []
            for carrier, voucher in sorted(numbers):
                handout = self._optional_read(carrier, self._key_path("handouts", voucher))
                if handout is None:
                    continue
                if handout != {"action_id": action_id, "voucher_id": voucher}:
                    raise _BadOutcome(reason, action_id=action_id, view="grant delivery handout", voucher_id=voucher)
                supplied.append(voucher)
            if len(supplied) != 1:
                raise _BadOutcome(reason, action_id=action_id, view="effective grant delivery", voucher_ids=supplied)

    def _observe_wave(
        self, release, topic_id, start, input_id, actions, dispatches, *, replays=None, aborted_ids=(), end=None
    ):
        stream = self.problem.stream
        topic = release["target_topic"]
        writer = release["job_id"]
        deadline = time.monotonic() + self._checkpoint_wait(release)
        while True:
            boundary = end if end is not None else self._output_ends(stream, topic)
            orders = self._output_interval(topic, topic_id, start, boundary)
            deliveries, missing = self._check_orders(
                orders, writer, topic_id, input_id, actions, dispatches, replays=replays, aborted_ids=aborted_ids
            )
            if not missing:
                self._await_order_deliveries(deliveries)
                logger.info(
                    "Airport meal production delivery job=%s topic_id=%s start=%s end=%s deliveries=%s",
                    writer,
                    topic_id,
                    start,
                    boundary,
                    len(deliveries),
                )
                return boundary
            if end is not None or time.monotonic() >= deadline:
                raise _ObservationUnavailable("attributed Flink output", job_id=writer, missing=sorted(missing))
            time.sleep(1)

    @staticmethod
    def _checkpoint_wait(release):
        interval = release["checkpoint_ms"] / 1000
        if interval <= 0:
            raise _ProbeError("Invalid production checkpoint interval")
        return min(150, max(30, interval + 45))

    def _completed_checkpoint(self, job_id):
        stream = self.problem.stream
        checkpoint = self._stream_call(stream.checkpoints, job_id)["latest"]["completed"]
        return checkpoint["id"] if checkpoint is not None else -1

    def _check_writer_progress(self, release, positions, checkpoint_before):
        """Confirm consumption when a replay can legitimately produce no new output."""
        stream = self.problem.stream
        job_id = release["job_id"]
        ends = {}
        for _, partition, offset in positions:
            ends[partition] = max(ends.get(partition, 0), offset + 1)
        deadline = time.monotonic() + self._checkpoint_wait(release)
        while True:
            observed = self._stream_call(stream.group_offsets, release["group"])
            if all(observed.get(p, -1) >= end for p, end in ends.items()):
                completed = self._completed_checkpoint(job_id)
                if completed > checkpoint_before:
                    self._production_release(job_id)
                    return
            if time.monotonic() >= deadline:
                raise _ObservationUnavailable(
                    "completed production checkpoint and input consumption",
                    job_id=job_id,
                    required=ends,
                    observed=observed,
                )
            time.sleep(1)

    def _exercise_stream(self, run_id, release):
        stream = self.problem.stream
        topic = release["target_topic"]
        topic_id = self._stream_call(stream.topic_id, topic)
        input_id = self._stream_call(stream.topic_id)
        output_start = self._output_ends(stream, topic)
        before = self._probe_actions(run_id, "before")
        buffered = self._probe_actions(run_id, "buffered", rotation=1)
        after = self._probe_actions(run_id, "after", rotation=2)
        tentative = self._probe_actions(run_id, "offer", rotation=3, kind="offer")
        aborted_ids = {row["action_id"] for row in tentative}

        self._submit(before)
        before_dispatches = self._await_dispatches(before)
        self._check_dispatch_coverage(before, before_dispatches, input_id)
        before_positions = [self._input_position(before_dispatches[row["action_id"]]) for row in before]
        initial_end = self._observe_wave(release, topic_id, output_start, input_id, before, before_dispatches)
        initial = self._check_new_issues(before)
        buffered_dispatches = {}

        def commit_while_stopped():
            self._submit(buffered)
            buffered_dispatches.update(self._await_dispatches(buffered))
            self._check_dispatch_coverage(buffered, buffered_dispatches, input_id)
            return self._output_ends(stream, topic)

        restored, old_end, stopped_end = self._resize(stream, release, commit_while_stopped)
        self._observe_wave(
            release,
            topic_id,
            initial_end,
            input_id,
            before,
            {},
            replays={row["action_id"]: (position,) for row, position in zip(before, before_positions, strict=True)},
            end=old_end,
        )
        if during_stop := self._output_interval(topic, topic_id, old_end, stopped_end):
            raise _BadOutcome(
                "live_issue_mismatch",
                view="output while production was stopped",
                positions=[position for position, _ in during_stop],
            )

        transaction = self._stream_call(stream.begin_transaction, zip(_PARTITIONS, tentative, strict=True))
        try:
            aborted_positions = [self._broker_position(row) for row in transaction.receipts]
            replay = self._stream_call(
                stream.produce,
                [(position[1], action) for position, action in zip(before_positions, before, strict=True)],
            )
            self._submit(after)
            after_dispatches = self._await_dispatches(after, timeout=30)
        finally:
            if self._stream_call(transaction.abort)["status"] != "aborted":
                raise _ProbeError("The test transaction did not report an abort")

        self._check_dispatch_coverage(after, after_dispatches, input_id)
        dispatches = before_dispatches | buffered_dispatches | after_dispatches
        after_positions = [self._input_position(row) for row in after_dispatches.values()]
        barriers = {(topic, partition): offset for topic, partition, offset in aborted_positions}
        redeliveries = {
            action["action_id"]: self._broker_position(row) for action, row in zip(before, replay, strict=True)
        }
        retries = {(topic, partition): offset for topic, partition, offset in redeliveries.values()}
        if any(not barriers[(topic, p)] < retries[(topic, p)] < offset for topic, p, offset in after_positions):
            raise _ProbeError("Fresh commits did not follow the aborted offers and redeliveries on every partition")

        actions = before + buffered + after
        replays = {
            row["action_id"]: (position, redeliveries[row["action_id"]])
            for row, position in zip(before, before_positions, strict=True)
        }
        restored_end = self._observe_wave(
            restored,
            topic_id,
            stopped_end,
            input_id,
            actions,
            buffered_dispatches | after_dispatches,
            replays=replays,
            aborted_ids=aborted_ids,
        )
        fresh_views = self._check_new_issues(actions)
        before_ids = before_dispatches.keys()
        for carrier, view in initial.carriers.items():
            _preserved(
                [row for row in view["handouts"] if row["action_id"] in before_ids],
                fresh_views.carriers[carrier]["handouts"],
                ("voucher_id",),
                "live_issue_mismatch",
            )
        self._check_aborted(aborted_ids, fresh_views)
        live = _LiveRoute(restored, topic_id, input_id, stopped_end, restored_end, replays)
        return actions, after, aborted_ids, dispatches, live

    def _exercise_changes(self, live, actions, after):
        revise = {}
        withdraw = {}
        for cafe in _CAFES:
            revised, withdrawn, *_ = [row for row in after if row["cafe"] == cafe]
            revise[cafe] = revised | {
                "action_id": f"{revised['action_id']}-revise",
                "kind": "revise",
                "revision": 2,
                "amount_cents": revised["amount_cents"] + 375,
            }
            withdraw[cafe] = withdrawn | {
                "action_id": f"{withdrawn['action_id']}-withdraw",
                "kind": "withdraw",
                "revision": 2,
                "amount_cents": 0,
            }
        changes = [*revise.values(), *withdraw.values()]
        dispatches = self._publish_probe(live, changes)
        views = self._check_new_issues(actions + changes)
        revised = {}
        withdrawn = {}
        for cafe, view in views.cafes.items():
            supplied = {row["voucher_id"] for state in views.carriers.values() for row in state["handouts"]}
            changed = revise[cafe]
            rows = [
                row
                for row in view["catalog"]
                if all(row[field] == changed[field] for field in _BENEFIT_KEY) and row["voucher_id"] in supplied
            ]
            if rows:
                revised[cafe] = (rows[-1], rows[0]["voucher_id"])
            withdrawn[cafe] = next(
                row["voucher_id"]
                for row in view["catalog"]
                if all(row[field] == withdraw[cafe][field] for field in _BENEFIT_KEY) and row["voucher_id"] in supplied
            )
        return changes, dispatches, revised, withdrawn

    def _publish_probe(self, live, actions):
        self._submit(actions)
        dispatches = self._await_dispatches(actions)
        self._check_dispatch_coverage(actions, dispatches, live.input_id)
        live.cursor = self._observe_wave(live.release, live.topic_id, live.cursor, live.input_id, actions, dispatches)
        return dispatches

    def _exercise_counter_redelivery(self, expected, live):
        reason = "counter_withdrawal_mismatch"
        case = expected["departure_cases"]
        original = next(row for row in expected["authorizations"] if row["action_id"] == case["supplied"]["action_id"])
        carrier = original["carrier"]
        action_id = original["action_id"]
        stream = self.problem.stream
        release = self._production_release(live.release["job_id"])
        topic = release["target_topic"]

        def effects():
            view = self._read(carrier, "/state")
            clearing = self._read("clearing", "/state")
            aliases = {
                row["voucher_id"]: row
                for row in clearing["aliases"]
                if all(row[field] == original[field] for field in _REVISION_KEY)
            }
            handouts = {row["voucher_id"]: row for row in view["handouts"] if row["action_id"] == action_id}
            return aliases, handouts

        self._check_counter(case)
        original_orders = [row for row in expected["clearing_orders"] if row["action_id"] == action_id]
        for delivery in original_orders:
            path = self._key_path("orders", *_output_position(delivery))
            if self._read("clearing", path, reason=reason) != delivery:
                raise _BadOutcome(reason, view="original grant delivery changed", action_id=action_id)
        if not original_orders:
            path = self._key_path("revisions", carrier, original["benefit_id"], original["revision"])
            if self._read("clearing", path, reason=reason)["action"] != original:
                raise _BadOutcome(reason, view="original grant authorization changed", action_id=action_id)
        before = effects()
        checkpoint_before = self._completed_checkpoint(release["job_id"])
        output_start = self._output_ends(stream, topic)
        partition = _desk_partition(carrier, original["benefit_id"])
        (sent,) = self._stream_call(stream.produce, [(partition, original)])
        position = self._broker_position(sent)
        historical_positions = {self._input_position(row) for row in original_orders}
        live.replays[action_id] = (position, *sorted(historical_positions))
        # The ordinary issuer may deliver another physical record. A publisher
        # that suppresses it must still durably consume the replay.
        output_end = self._output_ends(stream, topic)
        orders = self._output_interval(topic, live.topic_id, output_start, output_end)
        deliveries, _ = self._check_orders(
            orders, release["job_id"], live.topic_id, live.input_id, [original], {}, replays=live.replays
        )
        if not any(self._input_position(row) == position for row, _ in deliveries):
            self._check_writer_progress(release, [position], checkpoint_before)
            output_end = self._output_ends(stream, topic)
            orders = self._output_interval(topic, live.topic_id, output_start, output_end)
            deliveries, _ = self._check_orders(
                orders, release["job_id"], live.topic_id, live.input_id, [original], {}, replays=live.replays
            )
        self._await_order_deliveries(deliveries)
        self._check_counter(case)
        if effects() != before:
            raise _BadOutcome(reason, view="old grant redelivery changed voucher numbers or handouts")
        logger.info(
            "Airport meal counter redelivery job=%s input=%s deliveries=%s",
            release["job_id"],
            position,
            len(deliveries),
        )
        return original

    def _exercise_closes(self, run_id, catalogs):
        reason = "voucher_alias_mismatch"
        accepted = {}
        for cafe in _CAFES:
            revised = catalogs.get(cafe)
            if revised is not None:
                self._stage_current(cafe, revised[0]["voucher_id"])
            request_id = f"{run_id}-{cafe}-close"
            path = self._key_path("closes", request_id)
            before = self._read(cafe, "/state")
            attempts = {}
            for _ in range(3):
                request = {"request_id": request_id, "expected_catalog_revision": before["catalog_revision"]}
                attempts[before["catalog_revision"]] = before
                status, close = self._write_with_readback(cafe, "/closes", request, path)
                if status == 200:
                    break
                if status == 400:
                    raise _ProbeError(f"{cafe} rejected the close probe input: {close}")
                if status != 409:
                    raise _BadOutcome(reason, cafe=cafe, view="shift close", status=status, response=close)
                stored = self._optional_read(cafe, path)
                if stored is not None:
                    close = stored
                    break
                if close["error"] != "catalog revision changed":
                    raise _BadOutcome(reason, cafe=cafe, view="shift close", response=close)
                newer = self._read(cafe, "/state")
                if newer["catalog_revision"] == before["catalog_revision"]:
                    raise _BadOutcome(reason, cafe=cafe, view="shift close refused at current revision", response=close)
                before = newer
            else:
                raise _ObservationUnavailable("café close under concurrent updates", cafe=cafe, request_id=request_id)
            after = self._read(cafe, "/state")
            before = attempts.get(close["expected_catalog_revision"])
            request = {"request_id": request_id, "expected_catalog_revision": close["expected_catalog_revision"]}
            if (
                before is None
                or any(close[field] != value for field, value in request.items())
                or close["catalog_revision"] != request["expected_catalog_revision"] + 1
                or not before["receipt_seq"] <= close["receipt_seq"] <= after["receipt_seq"]
                or after["mode"] != "benefit"
                or after["catalog_revision"] < close["catalog_revision"]
                or self._read(cafe, path, reason=reason) != close
                or self._post(cafe, "/closes", request, reason, key_path=path) != close
            ):
                raise _BadOutcome(reason, cafe=cafe, view="shift close", close=close)
            accepted[cafe] = close
        return accepted

    def _stage_current(self, cafe, voucher_id):
        reason = "voucher_alias_mismatch"
        staged = self._key_path("staged", voucher_id)
        path = self._key_path("catalog", voucher_id)
        for _ in range(3):
            current = self._read(cafe, path, reason=reason)
            status, accepted = self._write_with_readback(cafe, "/staged", current, staged)
            if status == 400:
                raise _ProbeError(f"{cafe} rejected the staged catalog probe input: {accepted}")
            if status == 200:
                if accepted != current:
                    raise _BadOutcome(reason, cafe=cafe, voucher_id=voucher_id, view="staged catalogue")
                return
            if status != 409 or accepted["error"] != "catalog revision has advanced":
                raise _BadOutcome(reason, cafe=cafe, voucher_id=voucher_id, response=accepted)
            # Another revision can already be staged for the same alias.
            # Observe the normal close applying that newer work.
            newer = self._optional_read(cafe, staged)
            if newer is not None and newer["revision"] > current["revision"]:
                return
        raise _ObservationUnavailable("staging current café catalog", cafe=cafe, voucher_id=voucher_id)

    def _fresh_purchase(self, run_id, live, actions, dispatches, *, revised=False):
        for attempt in range(3):
            grant = self._probe_actions(run_id, f"fallback-{revised}-{attempt}")[0]
            dispatches.update(self._publish_probe(live, [grant]))
            actions.append(grant)
            if revised:
                change = grant | {
                    "action_id": f"{grant['action_id']}-revision",
                    "kind": "revise",
                    "revision": 2,
                    "amount_cents": grant["amount_cents"] + 375,
                }
                dispatches.update(self._publish_probe(live, [change]))
                actions.append(change)
            view = self._check_new_issues(actions).cafes[grant["cafe"]]
            catalog = next(
                (
                    row
                    for row in view["catalog"]
                    if row["benefit_id"] == grant["benefit_id"] and row["active"] and row["amount_cents"] > 0
                ),
                None,
            )
            if catalog is not None and (
                accepted := self._redeem_existing(grant["cafe"], catalog, f"{grant['action_id']}-sale")
            ):
                return accepted
        raise _ObservationUnavailable("fresh café sale under concurrent updates")

    def _withdrawn_sales(self, run_id, expected, withdrawn):
        case = expected["departure_cases"]
        counter = case["counter"]["action"]
        voucher = case["supplied"]["voucher_id"]
        self._check_counter(case)
        candidates = [(counter["cafe"], voucher), *withdrawn.items()]
        results = [
            self._must_refuse(cafe, number, f"{run_id}-{number}-withdrawn", withdrawn=True)
            for cafe, number in candidates
        ]
        return any(results)

    def _exercise(self, expected, precheck, release):
        run_id = uuid.uuid4().hex
        fresh, after, aborted_ids, dispatches, live = self._exercise_stream(run_id, release)
        changes, change_dispatches, revised, withdrawn = self._exercise_changes(live, fresh, after)
        actions = fresh + changes
        dispatches.update(change_dispatches)
        original = self._exercise_counter_redelivery(expected, live)
        before_close = self._history_views(expected, actions=actions)
        for cafe, catalog, other_voucher in self._alias_pairs(before_close):
            accepted = self._redeem_existing(cafe, catalog, f"{run_id}-{catalog['voucher_id']}-sale")
            if accepted is not None:
                sale, receipt = accepted
                alternate = other_voucher
                break
        else:
            sale, receipt = self._fresh_purchase(run_id, live, actions, dispatches)
            alternate = sale["voucher_id"]
        first_cafe = receipt["cafe"]
        self._must_refuse(first_cafe, alternate, f"{run_id}-duplicate-1", spent_voucher=sale["voucher_id"])
        payment = self._remit(receipt)

        closes = self._exercise_closes(run_id, revised)
        self._check_new_issues(actions, timeout=6)
        post_close = self._history_views(expected, actions=actions, receipts=[receipt])
        self._withdrawn_sales(f"{run_id}-before-restart", expected, withdrawn)
        self._exercise_carried_spends(post_close, run_id)
        receiver_before = self._post_departure_file(expected)
        keyed_reads = [(first_cafe, self._key_path("requests", sale["request_id"]))]
        counter = expected["departure_cases"]["counter"]["action"]
        keyed_reads.extend(
            (
                (counter["carrier"], self._key_path("actions", counter["action_id"])),
                ("clearing", self._key_path("revisions", *(counter[field] for field in _REVISION_KEY))),
            )
        )
        self._restart_partners([*_CARRIERS, *_CAFES, "clearing"], keyed_reads=keyed_reads)
        # A send after the rollout exercises the restarted desk worker.
        post_restart = self._probe_actions(run_id, "after-dispatch-restart")[0]
        dispatches.update(self._publish_probe(live, [post_restart]))
        actions.append(post_restart)
        if not self._withdrawn_sales(f"{run_id}-after-restart", expected, withdrawn):
            change = post_restart | {
                "action_id": f"{post_restart['action_id']}-withdraw",
                "kind": "withdraw",
                "revision": 2,
                "amount_cents": 0,
            }
            dispatches.update(self._publish_probe(live, [change]))
            actions.append(change)
            cafe = change["cafe"]
            catalog = self._check_new_issues(actions).cafes[cafe]["catalog"]
            voucher = next(row["voucher_id"] for row in catalog if row["benefit_id"] == change["benefit_id"])
            if not self._must_refuse(cafe, voucher, f"{run_id}-withdrawn-fallback", withdrawn=True):
                raise _ObservationUnavailable("withdrawn café catalog during fresh sale")
        if self._post_departure_file(expected) != receiver_before:
            raise _BadOutcome("receiver_file_mismatch", view="keyed post after restart")
        sale_path = self._key_path("requests", sale["request_id"])
        if (
            self._read(first_cafe, sale_path, reason="voucher_use_incorrect") != receipt
            or self._post(first_cafe, "/receipts", sale, "voucher_use_incorrect", key_path=sale_path) != receipt
        ):
            raise _BadOutcome("voucher_use_incorrect", cafe=first_cafe, voucher_id=sale["voucher_id"])
        self._must_refuse(first_cafe, alternate, f"{run_id}-duplicate-after-restart", spent_voucher=sale["voucher_id"])
        if self._remit(receipt) != payment:
            raise _BadOutcome("receipt_payment_mismatch", cafe=first_cafe, receipt_seq=receipt["receipt_seq"])

        new_receipts = [receipt]
        new_payments = [payment]
        for cafe, (catalog, other_voucher) in revised.items():
            if not catalog["active"]:
                continue
            if accepted := self._redeem_existing(cafe, catalog, f"{run_id}-{cafe}-revised-sale"):
                second_sale, second_receipt = accepted
                second_alternate = other_voucher
                break
        else:
            second_sale, second_receipt = self._fresh_purchase(run_id, live, actions, dispatches, revised=True)
            second_alternate = second_sale["voucher_id"]
        if not self._must_refuse(
            second_receipt["cafe"],
            second_alternate,
            f"{run_id}-duplicate-revision",
            spent_voucher=second_sale["voucher_id"],
        ):
            # Preserve the accepted sale even if its alias has since changed.
            new_receipts.append(second_receipt)
            new_payments.append(self._remit(second_receipt))
            second_sale, second_receipt = self._fresh_purchase(
                f"{run_id}-duplicate", live, actions, dispatches, revised=True
            )
            if not self._must_refuse(
                second_receipt["cafe"],
                second_sale["voucher_id"],
                f"{run_id}-duplicate-fallback",
                spent_voucher=second_sale["voucher_id"],
            ):
                raise _ObservationUnavailable("duplicate café sale under concurrent updates")

        new_receipts.append(second_receipt)
        new_payments.append(self._remit(second_receipt))
        if supported := self._exercise_supported_purchase(self._read_views(), run_id):
            new_receipts.append(supported[0])
            new_payments.append(supported[1])
        self._retry_carrier_records(expected, precheck)

        # Re-read the complete restored interval to include late repeats and
        # activity between the individual probe waves.
        self._observe_wave(
            live.release,
            live.topic_id,
            live.start,
            live.input_id,
            [*actions, original],
            {key: row for key, row in dispatches.items() if key not in live.replays},
            replays=live.replays,
            aborted_ids=aborted_ids,
        )
        self._check_new_issues(actions, timeout=6)
        final = self._history_views(expected, actions=actions, receipts=new_receipts)
        self._check_aborted(aborted_ids, final)
        self._require_reader_ownership(self.problem.stream, live.release["job_id"])
        probe_dispatches = dispatches
        for carrier, state in final.carriers.items():
            _preserved(
                [probe_dispatches[row["action_id"]] for row in actions if row["carrier"] == carrier],
                state["dispatches"],
                ("action_id",),
                "live_issue_mismatch",
            )
            _preserved(
                precheck.carriers[carrier]["amendments"],
                state["amendments"],
                ("request_id",),
                "carrier_history_changed",
            )
        _preserved(new_payments, final.clearing["remittances"], _RECEIPT_KEY, "receipt_payment_mismatch")
        for earlier in (precheck, before_close, post_close):
            for carrier, view in earlier.carriers.items():
                _preserved(
                    view["handouts"],
                    final.carriers[carrier]["handouts"],
                    ("voucher_id",),
                    "carrier_history_changed",
                )
            for cafe, view in earlier.cafes.items():
                _preserved(view["receipts"], final.cafes[cafe]["receipts"], _RECEIPT_KEY, "receipt_history_changed")
                _preserved(view["closes"], final.cafes[cafe]["closes"], ("request_id",), "receipt_history_changed")
            for collection, key in (
                ("remittances", _RECEIPT_KEY),
                ("uploads", _RECEIPT_KEY),
                ("settlement_events", ("sequence",)),
            ):
                _preserved(earlier.clearing[collection], final.clearing[collection], key, "receipt_payment_mismatch")
            _preserved(
                earlier.clearing_after_cafes["orders"],
                final.clearing_after_cafes["orders"],
                _output_position,
                "authorization_mismatch",
            )
            _preserved(
                earlier.clearing_after_cafes["aliases"],
                final.clearing_after_cafes["aliases"],
                ("voucher_id",),
                "voucher_alias_mismatch",
            )
        for cafe, close in closes.items():
            _preserved([close], final.cafes[cafe]["closes"], ("request_id",), "receipt_history_changed")
