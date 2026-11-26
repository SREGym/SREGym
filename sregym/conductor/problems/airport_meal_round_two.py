"""Export history and partner recovery for the airport departure close."""

import json
from collections import Counter
from urllib.parse import quote

from sregym.conductor.problems.airport_meal_history import (
    CARRIERS,
    ISSUER_GROUP,
    ORDERS_TOPIC,
    PlannedAction,
    _action,
    _catalog_row,
    _coverage,
    _current_actions,
    _ends,
    _identifier,
    _legacy_issue,
    _partner_many,
    _publish,
    _require,
    _stream_order,
    _submit,
    _wait,
    _wait_offsets,
    _wait_orders,
    partner_request,
)

CARRIER = "north"
CAFE = "gate-a"
PREVIEW = "preview-orders"
FILE_ID = "north-inbox-overnight"
SUPPORT_BANK = "remittance"
CASE_BANKS = ("transfer", "inbox", "counter", SUPPORT_BANK)


def _post(problem, site, path, body):
    return partner_request(problem, site, "POST", path, body, retry_transient=True)


def _get(problem, site, path):
    return partner_request(problem, site, "GET", path, retry_transient=True)


def _key(value):
    return quote(value, safe="")


def _export_editions(rows):
    actions = [
        {key: value for key, value in row.items() if not key.startswith(("input_", "output_"))}
        for row in rows
        if row["kind"] != "offer"
    ]
    return _current_actions(actions)


def capture_export(stream, label, topic, job, starts):
    """Capture an exclusive range while the designated writer owns the route."""
    release = stream.release("production" if topic == ORDERS_TOPIC else "rehearsal")
    _require(release["job_id"] == job and release["target_topic"] == topic, "Export writer does not own its route")
    ends = _ends(stream, topic)
    topic_id = stream.topic_id(topic)
    rows = stream.read_orders(topic, offsets=starts)
    _require(stream.topic_id(topic) == topic_id, "Export topic changed during capture")
    reference = {
        "range_id": label,
        "topic": topic,
        "topic_id": topic_id,
        "writer_job_id": job,
        "partitions": {str(p): {"start": start, "end": ends[p]} for p, start in starts.items()},
    }
    input_id = stream.topic_id()
    selected = [
        _stream_order(row, input_id, topic_id)
        for row in rows
        if starts[row["partition"]] <= row["offset"] < ends[row["partition"]]
    ]
    archive = {"reference": reference, "rows": selected}
    stream.archive(f"export-{label}", archive)
    return archive


def configure_banks(problem, banks, export, carriers=CARRIERS):
    requests = []
    for carrier in carriers:
        requests.append((carrier, "POST", "/exports", export))
        requests.extend(
            (carrier, "POST", "/close-settings", {"bank": bank, "range_id": export["reference"]["range_id"]})
            for bank in banks
        )
    _partner_many(problem, requests, retry_transient=True)


def close_banks(problem, banks, carriers=CARRIERS, *, admissions=None):
    admissions = admissions or {}
    return _partner_many(
        problem,
        [
            (
                carrier,
                "POST",
                "/closes",
                {
                    "close_id": _identifier(carrier, bank, "close"),
                    "statement_id": _identifier(carrier, bank, "statement"),
                    "bank": bank,
                    "contingency_admissions": admissions.get((carrier, bank), []),
                },
            )
            for carrier in carriers
            for bank in banks
        ],
        retry_transient=True,
    )


def prepare_cohorts(problem, owners, input_id, job, topic_ids):
    healthy, equal = [p for p, count in owners.items() if count == 1][:2]
    missing = next(p for p, count in owners.items() if count == 0)
    initial = [
        _action(healthy, 7100, CARRIER, "gate-b", "early", 1200),
        _action(missing, 7101, CARRIER, "gate-b", "early", 1300),
        _action(missing, 7102, CARRIER, "gate-b", "early", 1300),
        _action(equal, 7103, CARRIER, "gate-b", "early", 1150),
    ]
    later = [
        _action(healthy, 7100, CARRIER, "gate-b", "early", 1475, kind="revise", revision=2),
        _action(equal, 7104, CARRIER, "gate-b", "early", 1150),
    ]
    cases = [
        _action(healthy, 7200 + index, CARRIER, "gate-c", bank, 1500)
        for index, bank in enumerate(("transfer",) * 4 + ("inbox",) * 2 + ("counter",))
    ] + [_action(healthy, 7300 + index, CARRIER, CAFE, SUPPORT_BANK, 1400) for index in range(16)]
    stream = problem.stream
    input_starts = _ends(stream, "allowances")
    starts = _ends(stream)
    _submit(problem, initial + cases, retry_transient=True)
    acks = _publish(problem, initial + cases, input_id)
    orders = _wait_orders(stream, ORDERS_TOPIC, initial + cases, acks, owners, offsets=starts)
    case_ids = {row.action["action_id"] for row in cases}
    printed_orders = [row for row in orders if row["action"]["action_id"] in case_ids]
    # The independent café orders deliberately arrive after the departure
    # export has reached the broker. Capture the delivery cursor on both sides.
    printed = _legacy_issue(problem, printed_orders[:-1], topic_ids)
    before = _get(problem, "clearing", "/orders")
    output_id = topic_ids[1]
    pending = _stream_order(printed_orders[-1], input_id, output_id)
    _require(
        all(
            (row["output_topic_id"], row["output_partition"], row["output_offset"])
            != (output_id, pending["output_partition"], pending["output_offset"])
            for row in before
        ),
        "The café delivery control has already arrived",
    )
    (delivered,) = _legacy_issue(problem, printed_orders[-1:], topic_ids)
    stream.archive(
        "cafe-delivery-boundary",
        {"pending_order": pending, "peer_delivery": printed[-1], "delivered_order": delivered, "writer_job_id": job},
    )
    printed.append(delivered)
    return {
        "initial": initial,
        "later": later,
        "cases": cases,
        "dispatches": acks,
        "printed": {row["benefit_id"]: row for row in printed},
        "input_starts": input_starts,
        "equal_partition": equal,
        "coverage": [healthy, missing, equal],
    }


def admit_contingency(problem, cohort, input_id, rehearsal, rehearsal_starts, primary, later_acks):
    stream = problem.stream
    rehearsal_export = capture_export(stream, "rehearsal-overnight", PREVIEW, rehearsal, rehearsal_starts)
    rehearsal_savepoint = stream.stop(rehearsal, drain=False)
    _require(stream.release("rehearsal")["status"] == "FINISHED", "Rehearsal writer is still running")
    starts = _ends(stream, PREVIEW)
    _require(
        all(starts[int(p)] >= limits["end"] for p, limits in rehearsal_export["reference"]["partitions"].items()),
        "Fallback overlaps the rehearsal writer",
    )
    input_starts = dict(cohort["input_starts"])
    input_starts[cohort["equal_partition"]] = later_acks[1]["input_offset"]
    group = "meal-contingency"
    writer = stream.start(
        route="rehearsal",
        name="meals-contingency",
        group=group,
        start="explicit",
        offsets=input_starts,
        offset_reset="none",
        compression=True,
    )
    _coverage(stream, writer)
    source = stream.read_records("allowances", offsets=input_starts)
    positions = dict(input_starts)
    for row in source:
        positions[row["partition"]] = max(positions[row["partition"]], row["offset"] + 1)
    _wait_offsets(stream, group, positions)
    _wait_orders(
        stream,
        PREVIEW,
        [PlannedAction(row["partition"], json.loads(row["value"])) for row in source],
        [
            {"input_topic": row["topic"], "input_partition": row["partition"], "input_offset": row["offset"]}
            for row in source
        ],
        Counter(dict.fromkeys(input_starts, 1)),
        offsets=starts,
    )
    end = _ends(stream, PREVIEW)
    _require(sum(end[p] - starts[p] for p in starts) > 0, "Fallback wrote no output")
    fallback = capture_export(stream, "contingency-early", PREVIEW, writer, starts)
    stream.stop(writer, drain=False)

    healthy, uncovered, covered, prior = [row.action["benefit_id"] for row in cohort["initial"]]
    successor = cohort["later"][1].action["benefit_id"]
    groups = {"bank-remote": [healthy], "bank-transfer": [covered], "bank-connector": [prior, successor]}
    admissions = []
    for name, ids in groups.items():
        approval = _post(
            problem,
            CARRIER,
            "/export-approvals",
            {
                "approval_id": f"north-{name}",
                "cohort": name,
                "bank": "early",
                "benefit_ids": ids,
                "signed_by": "north-departure-desk",
            },
        )
        receipt = _post(
            problem,
            "clearing",
            "/export-receipts",
            {
                **{k: v for k, v in approval.items() if k != "signed_by"},
                "receipt_id": f"clearing-{name}",
                "reference": fallback["reference"],
                "rows": fallback["rows"],
            },
        )
        admissions.append({"approval_id": approval["approval_id"], "receipt_id": receipt["receipt_id"]})
        delivered = []
        for row in receipt["rows"]:
            path = f"/orders/{_key(row['output_topic_id'])}/{row['output_partition']}/{row['output_offset']}"
            delivered.append(_get(problem, "clearing", path))
        _partner_many(
            problem,
            [
                (row["carrier"], "POST", "/handouts", {"action_id": row["action_id"], "voucher_id": row["voucher_id"]})
                for row in delivered
                if row["voucher_id"] is not None
            ]
            + [
                (row["cafe"], "POST", "/catalog", _catalog_row(row, row))
                for row in delivered
                if row["voucher_id"] is not None
            ],
            retry_transient=True,
        )

    # The scope has been accepted at clearing. The carrier runs its
    # ordinary close with the primary reference configured earlier.
    closes = close_banks(problem, ("early",), admissions={(CARRIER, "early"): admissions})
    close = next(row for row in closes if row["close_id"] == _identifier(CARRIER, "early", "close"))
    _require(
        close["export_reference"] == primary["reference"] and close["contingency_admissions"] == admissions,
        "The early close did not retain its export selection and signed admission keys",
    )
    before = _export_editions(primary["rows"])
    after = _export_editions(fallback["rows"])
    statement = _get(problem, CARRIER, f"/statements/{_identifier(CARRIER, 'early', 'statement')}")
    statement_lines = {row["benefit_id"]: row for row in statement["lines"]}
    receipts = [
        _get(problem, "clearing", f"/export-receipts/{_key(member['receipt_id'])}")
        for member in close["contingency_admissions"]
    ]
    accepted = _export_editions([row for receipt in receipts for row in receipt["rows"]])
    _require(
        before[(CARRIER, healthy)]["revision"] == statement_lines[healthy]["revision"] == 1
        and accepted[(CARRIER, healthy)]["revision"] == 2
        and {(CARRIER, uncovered), (CARRIER, covered)} <= after.keys()
        and uncovered not in statement_lines
        and covered not in statement_lines
        and (CARRIER, uncovered) not in accepted
        and (CARRIER, covered) in accepted
        and prior in statement_lines
        and successor not in statement_lines
        and prior not in {key[1] for key in accepted}
        and statement_lines[prior]["amount_cents"] == accepted[(CARRIER, successor)]["amount_cents"],
        "The captured departure cohorts do not establish the three statement comparisons",
    )
    _require(
        fallback["rows"] and all(row["input_topic_id"] == input_id for row in fallback["rows"]),
        "Fallback did not retain its source topic ID",
    )
    return {
        "singly_owned": {"partition": cohort["coverage"][0], "benefit_id": healthy},
        "unowned": {"partition": cohort["coverage"][1], "primary": uncovered, "fallback": covered},
        "same_amount": {"primary": prior, "fallback": successor},
    }, rehearsal_savepoint


def _transfer(problem, bank, name, left, right):
    statement = _identifier(CARRIER, bank, "statement")
    return _post(
        problem,
        CARRIER,
        "/transfer-approvals",
        {
            "approval_id": name,
            "statement_id": statement,
            "signed_by": "north-accounts",
            "lines": [
                {"benefit_id": row.action["benefit_id"], "revision": row.action["revision"], "amount_cents": amount}
                for row, amount in ((left, -260), (right, 260))
            ],
        },
    )


def _amend(problem, statement, key, lines):
    account = _get(problem, CARRIER, f"/accounts/{_key(statement)}")
    request = {
        "request_id": key,
        "statement_id": statement,
        "expected_account_revision": account["account_revision"],
        "lines": lines,
    }
    # The write may have completed even if its reply was lost.
    return _post(problem, CARRIER, "/amendments", request)


def _support_approval(problem, number, release_after, entries):
    return _post(
        problem,
        CARRIER,
        "/support-approvals",
        {
            "approval_id": f"north-cafe-credit-{number}",
            "signed_by": "north-settlement",
            "cafe": CAFE,
            "release_after": release_after,
            "entries": [
                {
                    "support_id": f"{row['benefit_id']}-credit",
                    "statement_id": _identifier(CARRIER, SUPPORT_BANK, "statement"),
                    "benefit_id": row["benefit_id"],
                    "revision": row["revision"],
                    "amount_cents": amount,
                    "status": "pending",
                }
                for row, amount in entries
            ],
        },
    )


def seed_recovery_cases(problem, cohort, export):
    configure_banks(problem, CASE_BANKS, export, (CARRIER,))
    close_banks(problem, CASE_BANKS, (CARRIER,))
    prior, previous_credit, debit, credit, file_debit, file_credit, counter, *sales = cohort["cases"]
    history = _transfer(problem, "transfer", "north-transfer-118", prior, previous_credit)
    _amend(problem, history["statement_id"], "north-transfer-118-posting", history["lines"])
    _transfer(problem, "transfer", "north-transfer-121", debit, credit)
    authority = _transfer(problem, "inbox", "north-transfer-122", file_debit, file_credit)
    source = export["reference"]["range_id"]
    file = {
        "file_id": FILE_ID,
        "statement_id": authority["statement_id"],
        "range_id": source,
        "lines": [
            line | {"line_id": f"{FILE_ID}-{index}"}
            for index, line in enumerate([*authority["lines"], authority["lines"][1]], 1)
        ],
    }
    _post(problem, CARRIER, "/adjustment-files", file)
    path = f"/adjustment-files/{FILE_ID}"
    partner_request(problem, CARRIER, "POST_UNCERTAIN", f"{path}/post", {"through": file["lines"][0]["line_id"]})
    first = _wait(
        "keyed receiver debit after the interrupted reply",
        lambda: _get(problem, CARRIER, f"{path}/postings") or None,
        timeout=20,
    )
    _require(
        len(first) == 1 and first[0]["line_id"] == file["lines"][0]["line_id"],
        "The receiver did not retain the debit after the uncertain reply",
    )

    action = counter.action | {
        "revision": 2,
        "kind": "withdraw",
        "amount_cents": 0,
        "action_id": f"{counter.action['benefit_id']}/2",
    }
    _post(
        problem,
        CARRIER,
        "/counter-actions",
        {"record_id": "north-local-counter-37", "signed_by": "north-counter-supervisor", "action": action},
    )
    receipts = _partner_many(
        problem,
        [
            (
                CAFE,
                "POST",
                "/receipts",
                {
                    "request_id": f"{row.action['benefit_id']}-purchase",
                    "voucher_id": cohort["printed"][row.action["benefit_id"]]["voucher_id"],
                    "amount_cents": 1000,
                },
            )
            for row in sales
        ],
        retry_transient=True,
    )
    _require([row["receipt_seq"] for row in receipts] == list(range(1, 17)), "Unexpected café receipt sequence")
    _support_approval(problem, 1, 13, [(receipts[13], 300)])
    _support_approval(problem, 2, 15, [(receipts[14], 200), (receipts[15], 100)])
    _partner_many(
        problem,
        [("clearing", "POST", "/uploads", row) for row in receipts[:13] + [receipts[14]]],
        retry_transient=True,
    )
    _require(
        _post(problem, "clearing", "/remittances", {"cafe": CAFE})["receipt_seq"] == 13,
        "The remittance cursor did not stop before receipt 14",
    )
    return receipts, action


def statement_editions(state, clearing):
    """Project the charged edition of each benefit before account postings."""
    current = _current_actions([row for row in state["actions"] if row["kind"] != "offer"])
    actions = {row["action_id"]: row for row in state["actions"]}
    closes = {row["close_id"]: row for row in state["closes"]}
    exports = {row["reference"]["range_id"]: row for row in state["exports"]}
    approvals = {row["approval_id"]: row for row in state["export_approvals"]}
    accepted = {row["receipt_id"]: row for row in clearing["export_receipts"]}
    statements = {}
    for statement in state["statements"]:
        statement_id = statement["statement_id"]
        close = closes[statement["close_id"]]
        bank = close["bank"]
        members = close.get("contingency_admissions", ())
        if (
            members
            or bank in CASE_BANKS
            or (state["site"] == CARRIER and bank == "early" and "export_reference" in statement)
        ):
            selection = close["export_reference"]
            source = exports[selection["range_id"]]
            _require(
                source["reference"] == statement["export_reference"] == selection, f"Export differs for {statement_id}"
            )
            basis = _export_editions(
                [row for row in source["rows"] if row["bank"] == bank and row["carrier"] == state["site"]]
            )
            if bank in CASE_BANKS:
                basis = {key: actions[row["action_id"]] for key, row in basis.items()}
        else:
            basis = current
        fields = ("benefit_id", "revision", "amount_cents")
        lines = {row["benefit_id"]: {key: row[key] for key in fields} for row in basis.values() if row["bank"] == bank}
        for member in members:
            approval_id = member["approval_id"]
            approval = approvals.get(approval_id)
            receipt = accepted.get(member["receipt_id"])
            _require(
                approval is not None
                and receipt is not None
                and approval["bank"] == bank
                and approval["carrier"] == state["site"]
                and all(
                    receipt[key] == approval[key] for key in ("approval_id", "cohort", "bank", "carrier", "benefit_ids")
                ),
                f"Clearing receipt and carrier approval disagree for {approval_id}",
            )
            editions = {
                row["benefit_id"]: row
                for row in _export_editions(receipt["rows"]).values()
                if row["carrier"] == state["site"] and row["bank"] == bank
            }
            for benefit_id in approval["benefit_ids"]:
                edition = editions.get(benefit_id)
                if edition is not None:
                    lines[benefit_id] = {key: edition[key] for key in fields}
                elif benefit_id in lines:
                    lines[benefit_id]["amount_cents"] = 0
        statements[statement_id] = lines
    return statements


def statement_targets(state, clearing, *, editions=None):
    """Apply signed account changes to the historical charge projection."""
    if editions is None:
        editions = statement_editions(state, clearing)
    historical_early = {
        close["statement_id"]
        for close in state["closes"]
        if state["site"] == CARRIER and close["bank"] == "early" and "export_reference" in close
    }
    targets = {}
    for statement_id, basis in editions.items():
        lines = {benefit: row.copy() for benefit, row in basis.items()}
        transfers = [
            row
            for authority in state["transfer_approvals"]
            if authority["statement_id"] == statement_id
            for row in authority["lines"]
        ]
        credits = [
            row
            for approval in state["support_approvals"]
            for row in approval["entries"]
            if row["statement_id"] == statement_id
        ]
        # Earlier accepted support funds the charge if one exists. Outside
        # the frozen charge projection it has its own accepted amount.
        if statement_id in historical_early:
            signed_ids = {row["support_id"] for row in credits}
            credits.extend(
                row
                for row in state["support_requests"]
                if row["statement_id"] == statement_id
                and row["benefit_id"] not in basis
                and row["support_id"] not in signed_ids
            )
        for change in transfers + credits:
            benefit = change["benefit_id"]
            line = lines.setdefault(benefit, {"benefit_id": benefit, "revision": 0, "amount_cents": 0})
            line["revision"] = max(line["revision"], change["revision"])
            line["amount_cents"] += change["amount_cents"]
        targets[statement_id] = lines
    return targets


def recovery_baseline(state, clearing):
    prior = next(row for row in state["transfer_approvals"] if row["approval_id"] == "north-transfer-118")
    (counter,) = state["counter_actions"]
    action = counter["action"]
    (supplied,) = [row for row in state["handouts"] if row["action_id"] == f"{action['benefit_id']}/1"]
    (file,) = state["adjustment_files"]
    return {
        "prior_transfer": prior,
        "filename": file["file_id"],
        "postings": state["file_postings"],
        "counter": counter,
        "supplied": supplied,
        "support_approvals": state["support_approvals"],
        "export_receipts": clearing["export_receipts"],
    }


def post_receiver(problem):
    return _post(problem, CARRIER, f"/adjustment-files/{FILE_ID}/post", {})


def replay_counter(problem):
    pending = _get(problem, CARRIER, "/counter-actions")
    for record in pending:
        action = record["action"]
        issued = _post(problem, "clearing", "/orders/revision", action)
        _require(issued["action"] == action, "Clearing did not accept the counter action")
    return pending


def finish_counter(problem, pending):
    for record in pending:
        action = record["action"]
        _post(
            problem,
            CARRIER,
            "/counter-dispositions",
            {"action_id": action["action_id"], "record_id": record["record_id"], "disposition": "fulfilled-locally"},
        )


def _mirror_support(problem, approval):
    payment = _get(problem, "clearing", f"/remittances/{approval['cafe']}/{approval['release_after']}")
    for row in approval["entries"]:
        entry = _post(problem, CARRIER, f"/support/{_key(row['support_id'])}/post", {"release_payment": payment})
        _post(problem, "clearing", "/support", entry | {"carrier": CARRIER})


def settle_approved_receipts(problem, mode=None):
    first, later = sorted(_get(problem, CARRIER, "/support-approvals"), key=lambda row: row["release_after"])
    original = {row["receipt_seq"]: row for row in _get(problem, CAFE, "/receipts")}

    def remit_through(sequence, upload):
        _post(problem, "clearing", "/uploads", original[upload])
        boundary = {"cafe": CAFE, "receipt_seq": sequence}
        observed = _post(problem, "clearing", "/remittances", {"cafe": CAFE, "through_receipt_seq": sequence})
        _require(observed == boundary, "The approved receipt boundary was not reached")

    _mirror_support(problem, first)
    if mode == "funding-late":
        _post(problem, "clearing", "/uploads", original[16])
        _post(problem, "clearing", "/uploads", original[14])
        _post(problem, "clearing", "/remittances", {"cafe": CAFE})
    else:
        remit_through(15, 14)
    _mirror_support(problem, later)
    remit_through(16, 16)


def disturb_prior_transfer(problem):
    prior = next(
        row for row in _get(problem, CARRIER, "/transfer-approvals") if row["approval_id"] == "north-transfer-118"
    )
    statement_id = prior["statement_id"]
    _amend(
        problem,
        statement_id,
        "north-transfer-118-reverse",
        [row | {"amount_cents": -row["amount_cents"]} for row in prior["lines"]],
    )
    _amend(problem, statement_id, "north-transfer-118-rebook", prior["lines"])


def verify_counter_dispatch(problem):
    state = _get(problem, CARRIER, "/state")
    for record in state["counter_dispositions"]:
        action_id = record["action_id"]
        _require(
            all(row["action_id"] != action_id for row in state["dispatches"]),
            "Resumed dispatch sent a locally fulfilled action",
        )
    _wait_offsets(problem.stream, ISSUER_GROUP, _ends(problem.stream), topic=ORDERS_TOPIC)
