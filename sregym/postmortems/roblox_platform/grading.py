"""Outcome predicates over independently collected workload and durable state."""


def data_checks(*, shards, acknowledgments, players, must_process):
    rows = [row for shard in shards for row in shard["players"]]
    purchase_rows = [row for shard in shards for row in shard["purchases"]]
    session_rows = [row for shard in shards for row in shard["sessions"]]
    receipt_rows = [row for shard in shards for row in shard["receipts"]]
    purchases = {row[0]: row[1:] for row in purchase_rows}
    sessions = {row[0]: row[1] for row in session_rows}
    receipts = {row[0]: row[1] for row in receipt_rows}
    spent = {}
    for player, _ in purchases.values():
        spent[player] = spent.get(player, 0) + 1
    return {
        "identities_preserved": sorted((row[0], row[1]) for row in rows)
        == [(i, f"player-{i}") for i in range(players)],
        "balances_conserved": all(coins == 100000 - spent.get(player, 0) for player, _, coins in rows),
        "unique_transactions": len(purchases) == len(purchase_rows)
        and len(sessions) == len(session_rows)
        and len(receipts) == len(receipt_rows),
        "consistent_transactions": all(
            item == "experience-pass" and sessions.get(rid) == player for rid, (player, item) in purchases.items()
        )
        and all(purchases.get(rid, [None])[0] == player for rid, player in receipts.items()),
        "acknowledged_work_preserved": all(
            purchases.get(row["request_id"]) == [row["player"], "experience-pass"]
            and sessions.get(row["request_id"]) == row["player"]
            for row in acknowledgments
        ),
        "recovery_tail_processed": all(
            rid in purchases and receipts.get(rid) == purchases[rid][0] for rid in must_process
        ),
    }
