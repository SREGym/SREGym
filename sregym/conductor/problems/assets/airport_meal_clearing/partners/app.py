"""Airport meal partner service. Each site owns one SQLite database."""

import json
import os
import sqlite3
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import urlopen

CARRIERS = ("north", "south")
CAFES = ("gate-a", "gate-b", "gate-c", "gate-d")
SITES = {"carrier": CARRIERS, "cafe": CAFES, "clearing": ("clearing",)}
COLLECTIONS = {
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
        "close_settings",
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


class Error(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def require(condition, message, status=409):
    if not condition:
        raise Error(message, status)


def string(value):
    return isinstance(value, str) and bool(value) and "\x00" not in value


def integer(value):
    return type(value) is int and -(2**63) <= value < 2**63


def nonnegative(value):
    return integer(value) and value >= 0


def positive(value):
    return integer(value) and value > 0


def boolean(value):
    return type(value) is bool


def carrier(value):
    return value in CARRIERS


def cafe(value):
    return value in CAFES


def fields(value, required, optional=None):
    require(isinstance(value, dict), "expected a JSON object", 400)
    optional = optional or {}
    require(value.keys() <= required.keys() | optional.keys(), "unexpected body fields", 400)
    for name, validate in required.items():
        require(name in value and validate(value[name]), f"invalid or missing {name}", 400)
    for name in value.keys() & optional.keys():
        require(optional[name](value[name]), f"invalid {name}", 400)
    return value


ACTION = {
    "action_id": string,
    "benefit_id": string,
    "carrier": carrier,
    "cafe": cafe,
    "revision": positive,
    "amount_cents": nonnegative,
    "kind": lambda v: v in ("grant", "revise", "withdraw", "offer"),
    "bank": string,
}
ACTION_OPTIONAL = {"predecessor": string}
CATALOG = {
    "voucher_id": string,
    "carrier": carrier,
    "benefit_id": string,
    "revision": positive,
    "amount_cents": nonnegative,
    "active": boolean,
}
SALE = {"request_id": string, "voucher_id": string, "amount_cents": positive}
RECEIPT = {
    **SALE,
    "cafe": cafe,
    "carrier": carrier,
    "benefit_id": string,
    "revision": positive,
    "allowance_cents": nonnegative,
    "receipt_seq": positive,
    "catalog_revision": nonnegative,
}
SUPPORT = {
    "support_id": string,
    "statement_id": string,
    "benefit_id": string,
    "revision": positive,
    "amount_cents": positive,
    "status": lambda v: v in ("pending", "posted"),
}


def validate_action(action):
    require(action["kind"] != "withdraw" or action["amount_cents"] == 0, "withdrawals have zero cents", 400)
    require(action.get("predecessor") != action["benefit_id"], "a benefit cannot replace itself", 400)


def coordinate_fields(prefix):
    return (
        {f"{prefix}_topic": string, f"{prefix}_partition": nonnegative, f"{prefix}_offset": nonnegative},
        {f"{prefix}_topic_id": string},
    )


INPUT, INPUT_OPTIONAL = coordinate_fields("input")
OUTPUT, OUTPUT_TOPIC_ID = coordinate_fields("output")
OUTPUT_OPTIONAL = {**OUTPUT_TOPIC_ID, "writer_job_id": string}


def coord(row, prefix):
    topic = row.get(f"{prefix}_topic_id", row[f"{prefix}_topic"])
    return topic, row[f"{prefix}_partition"], row[f"{prefix}_offset"]


def benefit(row):
    return row["carrier"], row["benefit_id"]


def subset(row, keys):
    return {key: row[key] for key in keys if key in row}


def encode(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def number(value, minimum=1):
    require(value.isascii() and value.isdecimal() and len(value) <= 19, "invalid number in path", 400)
    result = int(value)
    require(nonnegative(result) and result >= minimum, "invalid number in path", 400)
    return result


class Records:
    def __init__(self, db):
        self.db = db

    def get(self, name, key):
        row = self.db.execute(
            "SELECT payload FROM records WHERE collection = ? AND key = ?", (name, encode(key))
        ).fetchone()
        return json.loads(row[0]) if row else None

    def read(self, name, key):
        row = self.get(name, key)
        require(row is not None, f"{name} record not found", 404)
        return row

    def save(self, name, key, row):
        self.db.execute(
            "INSERT INTO records VALUES (?, ?, ?) "
            "ON CONFLICT(collection, key) DO UPDATE SET payload = excluded.payload",
            (name, encode(key), encode(row)),
        )
        return row

    def add(self, name, key, row):
        existing = self.get(name, key)
        if existing is not None:
            require(existing == row, f"{name} key already has different contents")
            return existing
        return self.save(name, key, row)

    def rows(self, name):
        return [
            json.loads(row[0])
            for row in self.db.execute("SELECT payload FROM records WHERE collection = ? ORDER BY rowid", (name,))
        ]

    def clear(self, name):
        self.db.execute("DELETE FROM records WHERE collection = ?", (name,))


def validate_lines(lines, amount_type):
    for line in lines:
        fields(line, {"benefit_id": string, "revision": positive, "amount_cents": amount_type})
    require(len({line["benefit_id"] for line in lines}) == len(lines), "duplicate benefit", 400)


def adjust(account, lines, column):
    items = {line["benefit_id"]: line for line in account["lines"]}
    for line in lines:
        item = items.setdefault(
            line["benefit_id"],
            {
                "benefit_id": line["benefit_id"],
                "revision": line["revision"],
                "original_cents": 0,
                "support_cents": 0,
                "amended_cents": 0,
                "balance_cents": 0,
            },
        )
        item["revision"] = max(item["revision"], line["revision"])
        item[column] += line["amount_cents"]
        item["balance_cents"] += line["amount_cents"]
    account["lines"] = [items[key] for key in sorted(items)]
    return account


def post_support(records, support, release_payment=None):
    if support["status"] == "posted":
        return support
    account = records.read("accounts", support["statement_id"])
    account["account_revision"] += 1
    records.save("accounts", account["statement_id"], adjust(account, [support], "support_cents"))
    posted = {**support, "status": "posted", "account_revision": account["account_revision"]}
    if release_payment is not None:
        posted["release_payment"] = release_payment
    return records.save("support", support["support_id"], posted)


def check_support_payment(approval, payment, clearing_url):
    site, sequence = approval["cafe"], approval["release_after"]
    require(
        payment is not None and payment.get("cafe") == site and payment.get("receipt_seq") == sequence,
        "attach the clearing payment for this approval's café and receipt",
    )
    try:
        with urlopen(f"{clearing_url}/remittances/{site}/{sequence}", timeout=3) as response:
            observed = json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            raise Error("the approval's clearing payment is not posted", 409) from exc
        raise Error("the clearing payment could not be read", 503) from exc
    except (OSError, ValueError) as exc:
        raise Error("the clearing payment could not be read", 503) from exc
    require(payment == observed, "the attached payment differs from clearing records")


def carrier_action(records, site, body):
    action = fields(body, ACTION, ACTION_OPTIONAL)
    validate_action(action)
    require(action["carrier"] == site, "action belongs to another carrier")
    key = action["action_id"]
    existing = records.get("actions", key)
    if existing is not None:
        return records.add("actions", key, action)
    if action["kind"] != "offer":
        benefit_id = action["benefit_id"]
        current = records.get("_authorizations", benefit_id)
        if current is not None:
            require(action["cafe"] == current["cafe"], "a benefit keeps its café")
            require(action["revision"] > current["revision"], "authorization revision must advance")
            require(action["kind"] != "grant", "benefit has already been granted")
        else:
            require(
                action["kind"] == "grant" and action["revision"] == 1,
                "first authorization must be a grant at revision 1",
            )
        if "predecessor" in action:
            prior = records.read("_authorizations", action["predecessor"])
            require(prior["cafe"] == action["cafe"], "replacement must keep the predecessor café")
        records.save("_authorizations", benefit_id, action)
    return records.add("actions", key, action)


def export_range(body):
    fields(
        body,
        {
            "range_id": string,
            "topic": string,
            "topic_id": string,
            "writer_job_id": string,
            "partitions": lambda value: isinstance(value, dict) and set(value) == {"0", "1", "2", "3"},
        },
    )
    for limits in body["partitions"].values():
        fields(limits, {"start": nonnegative, "end": nonnegative})
        require(limits["start"] <= limits["end"], "export interval is reversed", 400)
    return body


def export_rows(rows, reference):
    require(isinstance(rows, list), "expected export rows", 400)
    positions = set()
    for row in rows:
        fields(row, {**ACTION, **INPUT, **OUTPUT, **INPUT_OPTIONAL, **OUTPUT_TOPIC_ID}, ACTION_OPTIONAL)
        validate_action(row)
        topic_id, partition, offset = coord(row, "output")
        limits = reference["partitions"].get(str(partition))
        require(
            row["output_topic"] == reference["topic"]
            and topic_id == reference["topic_id"]
            and limits is not None
            and limits["start"] <= offset < limits["end"],
            "row is outside the declared export",
        )
        require((partition, offset) not in positions, "duplicate export coordinate")
        positions.add((partition, offset))
    return rows


def statement_lines(rows, site, bank):
    lines = {}
    for row in rows:
        if row["carrier"] != site or row["bank"] != bank or row["kind"] == "offer":
            continue
        item = lines.get(row["benefit_id"])
        if item is None or item["revision"] < row["revision"]:
            item = lines[row["benefit_id"]] = {
                "benefit_id": row["benefit_id"],
                "revision": row["revision"],
                "amount_cents": 0,
            }
        if item["revision"] == row["revision"]:
            item["amount_cents"] += row["amount_cents"]
    return sorted(lines.values(), key=lambda row: row["benefit_id"])


def close_admissions(records, bank, members, clearing_url):
    approvals, receipts = set(), set()
    for member in members:
        fields(member, {"approval_id": string, "receipt_id": string})
        approval_id, receipt_id = member["approval_id"], member["receipt_id"]
        require(approval_id not in approvals and receipt_id not in receipts, "duplicate close admission", 400)
        approvals.add(approval_id)
        receipts.add(receipt_id)
        approval = records.read("export_approvals", approval_id)
        require(approval["bank"] == bank, "close admission belongs to another bank")
        try:
            with urlopen(f"{clearing_url}/export-receipts/{quote(receipt_id, safe='')}", timeout=3) as response:
                receipt = json.load(response)
        except HTTPError as exc:
            if exc.code == 404:
                raise Error("close admission has no clearing receipt", 409) from exc
            raise Error("close admission receipt could not be read", 503) from exc
        except (OSError, ValueError) as exc:
            raise Error("close admission receipt could not be read", 503) from exc
        require(
            receipt["receipt_id"] == receipt_id
            and subset(receipt, ("approval_id", "cohort", "carrier", "bank", "benefit_ids"))
            == subset(approval, ("approval_id", "cohort", "carrier", "bank", "benefit_ids")),
            "close admission receipt differs from carrier approval",
        )


def carrier_close(records, site, body, clearing_url):
    fields(
        body,
        {"close_id": string, "statement_id": string, "bank": string},
        {"contingency_admissions": lambda value: isinstance(value, list)},
    )
    key = body["close_id"]
    members = body.get("contingency_admissions", [])
    previous = records.get("closes", key)
    if previous is not None:
        require(
            subset(previous, ("close_id", "statement_id", "bank")) == subset(body, ("close_id", "statement_id", "bank"))
            and previous.get("contingency_admissions", []) == members,
            "close ID already has different contents",
        )
        return previous
    configuration = records.read("close_settings", body["bank"])
    export = records.read("exports", configuration["range_id"])
    close_admissions(records, body["bank"], members, clearing_url)
    reference = export["reference"]
    actions = [row for row in records.rows("actions") if row["bank"] == body["bank"]]
    dispatches = [records.get("dispatches", row["action_id"]) for row in actions]
    close = {
        **body,
        "contingency_admissions": members,
        "issue_topic": reference["topic"],
        "issue_run": reference["writer_job_id"],
        "export_reference": reference,
        "actions": actions,
        "dispatches": [row for row in dispatches if row is not None],
    }
    statement = {
        "statement_id": body["statement_id"],
        "close_id": key,
        "export_reference": reference,
        "lines": statement_lines(export["rows"], site, body["bank"]),
    }
    records.add("statements", body["statement_id"], statement)
    account = {"statement_id": body["statement_id"], "account_revision": 0, "lines": []}
    records.add("accounts", body["statement_id"], adjust(account, statement["lines"], "original_cents"))
    return records.add("closes", key, close)


def approve_export(records, site, body):
    fields(
        body,
        {
            "approval_id": string,
            "cohort": string,
            "bank": string,
            "signed_by": string,
            "benefit_ids": lambda value: isinstance(value, list) and bool(value) and all(map(string, value)),
        },
    )
    require(len(body["benefit_ids"]) == len(set(body["benefit_ids"])), "duplicate cohort member", 400)
    for benefit_id in body["benefit_ids"]:
        action = records.read("_authorizations", benefit_id)
        require(action["bank"] == body["bank"], "cohort member has another bank")
    return records.add("export_approvals", body["approval_id"], {**body, "carrier": site})


def admit_export(records, body):
    fields(
        body,
        {
            "receipt_id": string,
            "approval_id": string,
            "cohort": string,
            "bank": string,
            "carrier": carrier,
            "benefit_ids": lambda value: isinstance(value, list) and bool(value) and all(map(string, value)),
            "reference": lambda value: isinstance(value, dict),
            "rows": lambda value: isinstance(value, list),
        },
    )
    reference = export_range(body["reference"])
    rows = export_rows(body["rows"], reference)
    key = body["receipt_id"]
    request = {name: value for name, value in body.items() if name != "rows"}
    admitted = [
        row
        for row in rows
        if row["carrier"] == body["carrier"]
        and row["bank"] == body["bank"]
        and row["benefit_id"] in body["benefit_ids"]
    ]
    receipt = {**request, "rows": admitted}
    records.add("export_receipts", key, receipt)
    for row in admitted:
        issue(records, "legacy", row)
    return receipt


def post_file_line(records, file, line):
    file_id, line_id = file["file_id"], line["line_id"]
    key = (file_id, line_id)
    previous = records.get("file_postings", key)
    if previous is not None:
        return previous
    statement_id = file["statement_id"]
    account = records.read("accounts", statement_id)
    revision = account["account_revision"]
    account["account_revision"] += 1
    items = [subset(line, ("benefit_id", "revision", "amount_cents"))]
    posting_id = f"receiver:{encode(key)}"
    amendment = records.add(
        "amendments",
        posting_id,
        {
            "request_id": posting_id,
            "statement_id": statement_id,
            "expected_account_revision": revision,
            "account_revision": revision + 1,
            "lines": items,
        },
    )
    records.save("accounts", statement_id, adjust(account, items, "amended_cents"))
    return records.add(
        "file_postings", key, {"file_id": file_id, "line_id": line_id, "posting_id": amendment["request_id"]}
    )


def carrier_write(records, site, path, body, *, clearing_url="http://clearing:8080"):
    match path:
        case ("exports",):
            fields(body, {"reference": lambda v: isinstance(v, dict), "rows": lambda v: isinstance(v, list)})
            reference = export_range(body["reference"])
            export_rows(body["rows"], reference)
            return records.add("exports", reference["range_id"], body)
        case ("close-settings",):
            fields(body, {"bank": string, "range_id": string})
            records.read("exports", body["range_id"])
            return records.save("close_settings", body["bank"], body)
        case ("export-approvals",):
            return approve_export(records, site, body)
        case ("transfer-approvals",):
            fields(
                body,
                {
                    "approval_id": string,
                    "statement_id": string,
                    "signed_by": string,
                    "lines": lambda v: isinstance(v, list) and bool(v),
                },
            )
            validate_lines(body["lines"], integer)
            require(sum(row["amount_cents"] for row in body["lines"]) == 0, "transfer must balance")
            records.read("statements", body["statement_id"])
            for line in body["lines"]:
                records.read("_authorizations", line["benefit_id"])
            return records.add("transfer_approvals", body["approval_id"], body)
        case ("adjustment-files",):
            fields(
                body,
                {
                    "file_id": string,
                    "statement_id": string,
                    "range_id": string,
                    "lines": lambda v: isinstance(v, list) and bool(v),
                },
            )
            records.read("accounts", body["statement_id"])
            records.read("exports", body["range_id"])
            for line in body["lines"]:
                fields(line, {"line_id": string, "benefit_id": string, "revision": positive, "amount_cents": integer})
                records.read("_authorizations", line["benefit_id"])
            require(len({line["line_id"] for line in body["lines"]}) == len(body["lines"]), "duplicate file line ID")
            return records.add("adjustment_files", body["file_id"], body)
        case ("adjustment-files", file_id, "post"):
            fields(body, {}, {"through": string})
            file = records.read("adjustment_files", file_id)
            ids = [line["line_id"] for line in file["lines"]]
            through = body.get("through", ids[-1])
            require(through in ids, "unknown file line", 404)
            return [post_file_line(records, file, line) for line in file["lines"][: ids.index(through) + 1]]
        case ("counter-actions",):
            fields(body, {"record_id": string, "signed_by": string, "action": lambda v: isinstance(v, dict)})
            key = body["action"].get("action_id")
            previous = records.get("counter_actions", key)
            if previous is not None:
                return records.add("counter_actions", key, body)
            require(records.get("actions", key) is None, "counter handling must be declared at intake")
            carrier_action(records, site, body["action"])
            return records.add("counter_actions", key, body)
        case ("counter-dispositions",):
            fields(body, {"action_id": string, "record_id": string, "disposition": lambda v: v == "fulfilled-locally"})
            key = body["action_id"]
            counter = records.read("counter_actions", key)
            require(counter["record_id"] == body["record_id"], "wrong counter record")
            require(records.get("dispatches", key) is None, "action already dispatched")
            return records.add("counter_dispositions", key, body)
        case ("support-approvals",):
            fields(
                body,
                {
                    "approval_id": string,
                    "signed_by": string,
                    "cafe": cafe,
                    "release_after": nonnegative,
                    "entries": lambda v: isinstance(v, list) and bool(v),
                },
            )
            key = body["approval_id"]
            if records.get("support_approvals", key) is not None:
                return records.add("support_approvals", key, body)
            approved_ids = {
                entry["support_id"] for approval in records.rows("support_approvals") for entry in approval["entries"]
            }
            for entry in body["entries"]:
                fields(entry, SUPPORT)
                require(entry["status"] == "pending", "approval must start pending")
                require(entry["support_id"] not in approved_ids, "support already belongs to an approval")
                approved_ids.add(entry["support_id"])
                current = carrier_write(records, site, ("support",), entry)
                require(current["status"] == "pending", "support was already posted")
            return records.add("support_approvals", key, body)
        case ("actions",):
            return carrier_action(records, site, body)
        case ("dispatch-control",):
            fields(body, {"paused": boolean})
            return records.save("_settings", "dispatch", body)
        case ("dispatches",):
            fields(body, {"action_id": string, **INPUT}, INPUT_OPTIONAL)
            records.read("actions", body["action_id"])
            records.add("_dispatch_coordinates", coord(body, "input"), body["action_id"])
            return records.add("dispatches", body["action_id"], body)
        case ("handouts",):
            fields(body, {"action_id": string, "voucher_id": string})
            action = records.read("actions", body["action_id"])
            require(action["kind"] in ("grant", "revise"), "action cannot supply a voucher")
            return records.add("handouts", body["voucher_id"], body)
        case ("closes",):
            if "statement_id" in body:
                return carrier_close(records, site, body, clearing_url)
            fields(body, {"close_id": string, "bank": string, "issue_topic": string, "issue_run": string})
            key = body["close_id"]
            existing = records.get("closes", key)
            if existing is not None:
                require(subset(existing, body) == body, "close ID already has different contents")
                return existing
            actions = [row for row in records.rows("actions") if row["bank"] == body["bank"]]
            dispatches = [records.get("dispatches", row["action_id"]) for row in actions]
            manifest = {**body, "actions": actions, "dispatches": [row for row in dispatches if row is not None]}
            return records.add("closes", key, manifest)
        case ("statements",):
            fields(body, {"statement_id": string, "close_id": string, "lines": lambda v: isinstance(v, list)})
            validate_lines(body["lines"], nonnegative)
            key = body["statement_id"]
            close = records.read("closes", body["close_id"])
            require("export_reference" not in close, "this close compiles its own statement")
            statement = records.add("statements", key, body)
            if records.get("accounts", key) is None:
                account = {"statement_id": key, "account_revision": 0, "lines": []}
                records.add("accounts", key, adjust(account, body["lines"], "original_cents"))
            return statement
        case ("support",):
            fields(body, SUPPORT)
            key = body["support_id"]
            records.read("accounts", body["statement_id"])
            records.add("support_requests", key, body)
            current = records.get("support", key)
            if current is not None:
                return current
            entry = {**body, "status": "pending", "account_revision": None}
            if body["status"] == "posted":
                return post_support(records, entry)
            return records.add("support", key, entry)
        case ("support", support_id, "post"):
            fields(body, {}, {"release_payment": lambda value: isinstance(value, dict)})
            support = records.read("support", support_id)
            payment = body.get("release_payment")
            if support["status"] == "posted":
                require(payment == support.get("release_payment"), "retry must use the original payment")
                return support
            approval = next(
                (
                    row
                    for row in records.rows("support_approvals")
                    if any(entry["support_id"] == support_id for entry in row["entries"])
                ),
                None,
            )
            if approval is None:
                require(payment is None, "support has no approval with a payment requirement")
            else:
                check_support_payment(approval, payment, clearing_url)
            return post_support(records, support, payment)
        case ("amendments",):
            fields(
                body,
                {
                    "request_id": string,
                    "statement_id": string,
                    "expected_account_revision": nonnegative,
                    "lines": lambda v: isinstance(v, list) and bool(v),
                },
                {"transfer_approval_id": string},
            )
            validate_lines(body["lines"], integer)
            key = body["request_id"]
            require(not key.startswith("receiver:"), "posting key belongs to the receiver", 400)
            existing = records.get("amendments", key)
            if existing is not None:
                require(subset(existing, body) == body, "amendment ID already has different contents")
                require(
                    existing.get("transfer_approval_id") == body.get("transfer_approval_id"),
                    "retry must use the original transfer approval",
                )
                return existing
            if approval_id := body.get("transfer_approval_id"):
                approval = records.read("transfer_approvals", approval_id)
                require(approval["statement_id"] == body["statement_id"], "transfer belongs to another statement")
                applied = {
                    line["benefit_id"]
                    for row in records.rows("amendments")
                    if row.get("transfer_approval_id") == approval_id
                    for line in row["lines"]
                }
                require(
                    all(line in approval["lines"] and line["benefit_id"] not in applied for line in body["lines"]),
                    "transfer line is not approved or is already posted",
                )
            account = records.read("accounts", body["statement_id"])
            require(account["account_revision"] == body["expected_account_revision"], "account revision changed")
            account["account_revision"] += 1
            records.save("accounts", account["statement_id"], adjust(account, body["lines"], "amended_cents"))
            return records.add("amendments", key, {**body, "account_revision": account["account_revision"]})
    raise Error("unknown endpoint", 404)


def issuance_cutover(records):
    existing = records.get("_settings", "issuance_cutover")
    if existing is not None:
        return existing
    # Orders append in the same SQLite journal. This is the exclusive end
    # of the legacy issuance period, even if a legacy worker later redelivers.
    return records.add("_settings", "issuance_cutover", {"orders_through": len(records.rows("orders"))})


def issue(records, mode, body):
    has_coordinates = mode == "legacy" or bool(
        body.keys() & (INPUT.keys() | INPUT_OPTIONAL.keys() | OUTPUT.keys() | OUTPUT_OPTIONAL.keys())
    )
    required = {**ACTION, **INPUT, **OUTPUT} if has_coordinates else ACTION
    optional = {**ACTION_OPTIONAL, **INPUT_OPTIONAL, **OUTPUT_OPTIONAL} if has_coordinates else ACTION_OPTIONAL
    fields(body, required, optional)
    action = subset(body, ACTION.keys() | ACTION_OPTIONAL.keys())
    validate_action(action)
    if mode == "revision" and action["kind"] != "offer":
        issuance_cutover(records)
    output = coord(body, "output") if has_coordinates else None
    delivered = records.get("orders", output) if has_coordinates else None
    if delivered is not None:
        require(subset(delivered, body) == body, "output coordinate already has different contents")
        if mode == "legacy":
            return delivered

    records.add("_action_ids", (action["carrier"], action["action_id"]), action)
    voucher_id = None
    kind = action["kind"]
    if kind == "offer":
        records.add("offers", (action["carrier"], action["action_id"]), action)
    else:
        key = benefit(action)
        revision_key = (*key, action["revision"])
        records.add("_seen_revisions", revision_key, action)
        current = records.get("benefits", key)
        if current is not None:
            require(current["cafe"] == action["cafe"], "a benefit keeps its café")
        if current is None or current["revision"] < action["revision"]:
            records.save("benefits", key, action)

        committed = records.get("revisions", revision_key)
        first_alias = records.get("_first_alias", revision_key)
        if kind in ("grant", "revise"):
            if committed is not None or (mode == "revision" and first_alias is not None):
                voucher_id = first_alias
            else:
                serial = (records.get("_settings", "last_voucher") or 0) + 1
                voucher_id = f"V-{serial:08d}"
                records.save("_settings", "last_voucher", serial)
                records.add(
                    "aliases",
                    voucher_id,
                    {
                        **subset(action, ("carrier", "benefit_id", "cafe", "revision", "amount_cents")),
                        "voucher_id": voucher_id,
                    },
                )
                if first_alias is None:
                    records.add("_first_alias", revision_key, voucher_id)
        if mode == "revision":
            records.add("revisions", revision_key, {"action": action, "voucher_id": voucher_id})

    if has_coordinates and delivered is None:
        delivered = records.add("orders", output, {**body, "mode": mode, "voucher_id": voucher_id})
    if mode == "legacy":
        return delivered
    return {"action": action, "voucher_id": voucher_id}


def cursor(records, site):
    return records.get("cursors", site) or {"cafe": site, "receipt_seq": 0}


def append_settlement(records, kind, row):
    identity = subset(row, ("carrier", "support_id") if kind == "support" else ("cafe", "receipt_seq"))
    key = (kind, *identity.values())
    if records.get("settlement_events", key) is not None:
        return
    sequence = records.read("_settings", "settlement_sequence") + 1
    records.add("settlement_events", key, {"sequence": sequence, "kind": kind, **identity})
    records.save("_settings", "settlement_sequence", sequence)


def initialize_settlement(records):
    if records.get("_settings", "settlement_sequence") is not None:
        return
    records.add("_settings", "settlement_sequence", 0)
    # Older clearing volumes recorded both kinds in the same table. These rows
    # were inserted under the write lock and never replaced or deleted.
    rows = records.db.execute(
        "SELECT collection, payload FROM records WHERE collection IN ('support', 'remittances') ORDER BY rowid"
    ).fetchall()
    for collection, payload in rows:
        append_settlement(records, "support" if collection == "support" else "payment", json.loads(payload))


def remit(records, site, through_receipt_seq=None):
    progress = cursor(records, site)
    if through_receipt_seq is not None and through_receipt_seq <= progress["receipt_seq"]:
        payment = records.read("remittances", (site, through_receipt_seq))
        return subset(payment, ("cafe", "receipt_seq"))

    support = records.rows("support")
    sequence = progress["receipt_seq"] + 1
    while through_receipt_seq is None or sequence <= through_receipt_seq:
        receipt = records.get("uploads", (site, sequence))
        if receipt is None:
            break
        key = benefit(receipt)
        covered = 0
        if records.get("_first_payment", key) is None and receipt["allowance_cents"] > 0:
            covered = min(receipt["amount_cents"], receipt["allowance_cents"])
            records.add("_first_payment", key, (site, sequence))
        support_cents = 0
        support_ids = []
        for entry in support:
            if support_cents == covered:
                break
            if benefit(entry) == key:
                support_cents += min(covered - support_cents, entry["amount_cents"])
                support_ids.append(entry["support_id"])
        payment = {
            **subset(receipt, ("cafe", "receipt_seq", "carrier", "benefit_id", "amount_cents")),
            "carrier_cents": covered - support_cents,
            "support_cents": support_cents,
            "operator_cents": receipt["amount_cents"] - covered,
            "support_ids": support_ids,
        }
        records.add("remittances", (site, sequence), payment)
        append_settlement(records, "payment", payment)
        sequence += 1
    return records.save("cursors", site, {"cafe": site, "receipt_seq": sequence - 1})


def clearing_write(records, path, body):
    match path:
        case ("issuance-cutover",):
            fields(body, {})
            return issuance_cutover(records)
        case ("export-receipts",):
            return admit_export(records, body)
        case ("orders", "legacy" | "revision" as mode):
            return issue(records, mode, body)
        case ("support",):
            fields(
                body,
                {**SUPPORT, "carrier": carrier, "account_revision": positive},
                {"release_payment": lambda value: isinstance(value, dict)},
            )
            require(body["status"] == "posted", "support must be posted at the carrier")
            if (payment := body.get("release_payment")) is not None:
                require(
                    cafe(payment.get("cafe")) and positive(payment.get("receipt_seq")),
                    "release payment needs a café and receipt sequence",
                    400,
                )
                require(
                    payment == records.get("remittances", (payment["cafe"], payment["receipt_seq"])),
                    "the attached payment differs from clearing records",
                )
            support = records.add("support", (body["carrier"], body["support_id"]), body)
            append_settlement(records, "support", support)
            return support
        case ("uploads",):
            fields(body, RECEIPT)
            alias = records.read("aliases", body["voucher_id"])
            require(
                benefit(alias) == benefit(body) and alias["cafe"] == body["cafe"],
                "receipt does not match the printed voucher",
            )
            require(body["amount_cents"] <= body["allowance_cents"], "receipt exceeds the allowance")
            records.add("_upload_requests", (body["cafe"], body["request_id"]), body["receipt_seq"])
            return records.add("uploads", (body["cafe"], body["receipt_seq"]), body)
        case ("remittances",):
            fields(body, {"cafe": cafe}, {"through_receipt_seq": positive})
            return remit(records, body["cafe"], body.get("through_receipt_seq"))
    raise Error("unknown endpoint", 404)


def cafe_state(records):
    return records.get("_settings", "shift") or {
        "mode": "voucher",
        "catalog_revision": 0,
        "receipt_seq": 0,
    }


def authorization(row):
    return subset(row, ("revision", "amount_cents", "active"))


def newest(current, candidate):
    if current is None or current["revision"] < candidate["revision"]:
        return candidate
    if current["revision"] == candidate["revision"]:
        require(authorization(current) == authorization(candidate), "catalog revision already has different contents")
    return current


def update_catalog(records, collection, row):
    fields(row, CATALOG)
    voucher_id = row["voucher_id"]
    records.add("_voucher_identities", voucher_id, subset(row, ("carrier", "benefit_id")))
    current = records.get(collection, voucher_id)
    require(current is None or current["revision"] <= row["revision"], "catalog revision has advanced")
    newest(current, row)
    if collection == "catalog" and cafe_state(records)["mode"] == "benefit":
        key = benefit(row)
        current = records.get("_catalog_benefits", key)
        require(current is None or current["revision"] <= row["revision"], "benefit revision has advanced")
        latest = newest(current, authorization(row))
        records.save("_catalog_benefits", key, latest)
        if current is not None and current != latest:
            for alias in records.rows("catalog"):
                if benefit(alias) == key:
                    records.save("catalog", alias["voucher_id"], {**alias, **latest})
    return records.save(collection, voucher_id, row)


def close_cafe(records, body):
    fields(body, {"request_id": string, "expected_catalog_revision": nonnegative})
    request_id = body["request_id"]
    existing = records.get("closes", request_id)
    if existing is not None:
        require(subset(existing, body) == body, "close ID already has different contents")
        return existing

    shift = cafe_state(records)
    require(shift["catalog_revision"] == body["expected_catalog_revision"], "catalog revision changed")
    catalog = {row["voucher_id"]: row for row in records.rows("catalog")}
    for row in records.rows("staged"):
        voucher_id = row["voucher_id"]
        catalog[voucher_id] = newest(catalog.get(voucher_id), row)
    latest = {}
    for row in catalog.values():
        key = benefit(row)
        latest[key] = newest(latest.get(key), authorization(row))
    for key, row in latest.items():
        records.save("_catalog_benefits", key, row)
    for voucher_id, row in catalog.items():
        records.save("catalog", voucher_id, {**row, **latest[benefit(row)]})
    records.clear("staged")
    shift["mode"] = "benefit"
    shift["catalog_revision"] += 1
    records.save("_settings", "shift", shift)
    return records.add("closes", request_id, {**body, **subset(shift, ("catalog_revision", "receipt_seq"))})


def sales_control(records):
    return records.get("_settings", "sales") or {"paused": False}


def redeem(records, site, body):
    fields(body, SALE)
    key = body["request_id"]
    accepted = records.get("_accepted_requests", key)
    if accepted is not None:
        existing = records.read("receipts", accepted)
        require(subset(existing, body) == body, "sale request already has different contents")
        return existing

    require(not sales_control(records)["paused"], "sales are paused")
    voucher_id = body["voucher_id"]
    alias = records.read("catalog", voucher_id)
    require(alias["active"], "voucher is inactive")
    require(body["amount_cents"] <= alias["amount_cents"], "sale exceeds the allowance")
    shift = cafe_state(records)
    spend = benefit(alias)
    if shift["mode"] == "voucher":
        require(records.get("_spent_vouchers", voucher_id) is None, "voucher already used")
    else:
        require(records.get("_spent_benefits", spend) is None, "benefit already used")

    shift["receipt_seq"] += 1
    sequence = shift["receipt_seq"]
    receipt = {
        **body,
        **subset(alias, ("carrier", "benefit_id", "revision")),
        "cafe": site,
        "allowance_cents": alias["amount_cents"],
        "receipt_seq": sequence,
        "catalog_revision": shift["catalog_revision"],
    }
    records.add("receipts", sequence, receipt)
    records.add("_accepted_requests", key, sequence)
    records.add("_spent_vouchers", voucher_id, sequence)
    if records.get("_spent_benefits", spend) is None:
        records.add("_spent_benefits", spend, sequence)
    records.save("_settings", "shift", shift)
    return receipt


def cafe_write(records, site, path, body):
    match path:
        case ("catalog" | "staged" as collection,):
            return update_catalog(records, collection, body)
        case ("closes",):
            return close_cafe(records, body)
        case ("receipts",):
            return redeem(records, site, body)
        case ("sales-control",):
            fields(body, {"paused": boolean})
            return records.save("_settings", "sales", body)
    raise Error("unknown endpoint", 404)


def dispatch_control(records):
    return records.get("_settings", "dispatch") or {"paused": False}


def read(records, role, site, path):
    collections = COLLECTIONS[role]
    if path == ("state",):
        state = {"role": role, "site": site}
        state.update({name: records.rows(name) for name in collections})
        if role == "carrier":
            state["dispatch_paused"] = dispatch_control(records)["paused"]
        elif role == "cafe":
            state.update(cafe_state(records))
            state["sales_paused"] = sales_control(records)["paused"]
        else:
            state["cursors"] = [cursor(records, name) for name in CAFES]
            state["issuance_cutover"] = records.get("_settings", "issuance_cutover")
        return state
    if len(path) == 1:
        collection = path[0].replace("-", "_")
        if collection == "cursors" and role == "clearing":
            return [cursor(records, name) for name in CAFES]
        if collection in collections:
            return records.rows(collection)
    if role == "carrier":
        if path == ("dispatch-control",):
            return dispatch_control(records)
        if len(path) == 3 and path[0] == "adjustment-files" and path[2] == "postings":
            file = records.read("adjustment_files", path[1])
            return [row for row in records.rows("file_postings") if row["file_id"] == file["file_id"]]
        if len(path) == 2 and path[0].replace("-", "_") in collections:
            return records.read(path[0].replace("-", "_"), path[1])
    if role == "clearing":
        match path:
            case ("issuance-cutover",):
                return records.read("_settings", "issuance_cutover")
            case ("export-receipts", receipt_id):
                return records.read("export_receipts", receipt_id)
            case ("aliases", voucher_id):
                return records.read("aliases", voucher_id)
            case ("benefits" | "offers" | "support" as collection, carrier_id, key):
                return records.read(collection, (carrier_id, key))
            case ("revisions", carrier_id, benefit_id, revision):
                return records.read("revisions", (carrier_id, benefit_id, number(revision)))
            case ("orders", topic, partition, offset):
                return records.read("orders", (topic, number(partition, 0), number(offset, 0)))
            case ("uploads" | "remittances" as collection, cafe_id, sequence):
                return records.read(collection, (cafe_id, number(sequence)))
            case ("cursors", cafe_id):
                require(cafe(cafe_id), "unknown café", 404)
                return cursor(records, cafe_id)
    if role == "cafe":
        match path:
            case ("catalog" | "staged" | "closes" as collection, key):
                return records.read(collection, key)
            case ("receipts", sequence):
                return records.read("receipts", number(sequence))
            case ("requests", request_id):
                sequence = records.read("_accepted_requests", request_id)
                return records.read("receipts", sequence)
            case ("sales-control",):
                return sales_control(records)
    raise Error("unknown endpoint", 404)


class PartnerServer(ThreadingHTTPServer):
    def __init__(self, address, role, site, data_dir, *, clearing_url="http://clearing:8080"):
        if site not in SITES.get(role, ()):
            raise ValueError(f"invalid ROLE/SITE combination: {role}/{site}")
        self.role = role
        self.site = site
        self.clearing_url = clearing_url
        path = Path(data_dir)
        path.mkdir(parents=True, exist_ok=True)
        self.database = path / "partner.sqlite3"
        # Keep the WAL open across requests so the last request connection
        # does not force a checkpoint on every close.
        self.anchor = db = self.connect()
        try:
            mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode != "wal":
                raise RuntimeError("partner database requires SQLite WAL")
            with db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS records ("
                    "collection TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,"
                    "PRIMARY KEY(collection, key))"
                )
                records = Records(db)
                records.add("_settings", "owner", {"role": role, "site": site})
                if role == "clearing":
                    initialize_settlement(records)
            super().__init__(address, Handler)
        except BaseException:
            db.close()
            raise

    def server_close(self):
        super().server_close()
        self.anchor.close()

    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.execute("PRAGMA synchronous=FULL")
        return db


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch(write=True)

    def body(self):
        length = self.headers.get("Content-Length", "")
        require(
            length.isascii() and length.isdecimal() and len(length) <= 7 and 0 < int(length) <= 1024 * 1024,
            "expected a JSON body up to 1 MiB",
            400,
        )
        try:
            body = json.loads(self.rfile.read(int(length)))
        except (ValueError, RecursionError) as exc:
            raise Error("invalid JSON body") from exc
        require(isinstance(body, dict), "expected a JSON object", 400)
        return body

    def dispatch(self, write=False):
        role, site = self.server.role, self.server.site
        try:
            try:
                url = urlsplit(self.path)
            except ValueError as exc:
                raise Error("invalid path") from exc
            path = tuple(unquote(part) for part in url.path.lstrip("/").split("/"))
            body = self.body() if write else None
            with closing(self.server.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                records = Records(db)
                if write:
                    if role == "carrier":
                        result = carrier_write(records, site, path, body, clearing_url=self.server.clearing_url)
                    elif role == "clearing":
                        result = clearing_write(records, path, body)
                    else:
                        result = cafe_write(records, site, path, body)
                elif path == ("health",):
                    records.read("_settings", "owner")
                    result = {"ok": True, "role": role, "site": site, "sqlite_version": sqlite3.sqlite_version}
                else:
                    result = read(records, role, site, path)
            self.reply(200, result)
        except Error as exc:
            self.reply(exc.status, {"error": str(exc)})
        except sqlite3.Error as exc:
            self.log_error("database error: %s", exc)
            self.reply(503, {"error": "partner database unavailable"})

    def reply(self, status, result):
        payload = encode(result).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == "__main__":
    with PartnerServer(("0.0.0.0", 8080), os.environ["ROLE"], os.environ["SITE"], os.environ["DATA_DIR"]) as server:
        server.serve_forever()
