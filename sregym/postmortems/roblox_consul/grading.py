"""Outcome predicates, kept outside the agent and service images."""


def evaluate(samples, *, window, rps, rows, expected, overloads, initial_overloads, voters, prior_incorrect):
    totals = {k: sum(s[k] for s in samples) for k in ("success", "failed", "maintenance", "incorrect")}
    attempted = sum(totals.values())
    checks = {
        "full_admission": bool(samples) and all(s["admission"] == 100 for s in samples),
        "sustained_success": attempted >= max(1, len(samples)) * rps * 0.8
        and totals["success"] / max(1, attempted) >= 0.98,
        "observation_window": len(samples) >= max(2, int(window * 0.65)),
        "player_data_preserved": rows == expected,
        "correct_responses": totals["incorrect"] == 0 and not prior_incorrect,
        "no_origin_overload": overloads == initial_overloads,
        "three_voters": voters == 3,
    }
    return {"passed": all(checks.values()), "checks": checks, "totals": totals}
