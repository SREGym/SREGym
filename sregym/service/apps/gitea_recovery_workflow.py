"""Issue journal operations, also executed with the real API inside the client pod."""

import json


def create_issues(api, records):
    receipts = []
    for record in records:
        issue = api(
            f"/repos/{record['repository']}/issues",
            "POST",
            {"title": record["title"], "body": record["body"]},
        )
        receipts.append({**record, "number": issue["number"]})
    return receipts


def reconcile_issues(api, receipts, write=False):
    """Replay only absent records, preserving public issue numbers and content.

    A conflicting issue is an integrity error, not permission to overwrite it.
    Receipt order preserves the per-repository allocation order of issue numbers.
    """
    created = 0
    for receipt in receipts:
        path = f"/repos/{receipt['repository']}/issues"
        issue = api(f"{path}/{receipt['number']}", missing_ok=True)
        if issue is None and write:
            issue = api(path, "POST", {"title": receipt["title"], "body": receipt["body"]})
            created += 1
        if issue is None or any(issue.get(key) != receipt[key] for key in ("number", "title", "body")):
            raise RuntimeError(f"Issue receipt not reconciled: {receipt['repository']}#{receipt['number']}")
    return {"verified": len(receipts), "created": created}


def run_recovery(api, mode, **arguments):
    if mode == "create":
        result = create_issues(api, arguments["records"])
    elif mode in {"replay", "verify"}:
        result = reconcile_issues(api, arguments["receipts"], write=mode == "replay")
    else:
        raise ValueError(mode)
    print(json.dumps(result))
