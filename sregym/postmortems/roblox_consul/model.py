"""Explicit approximations, NOT a reproduction of upstream Consul/BoltDB bugs.

All time constants are compressed laboratory values. The streaming queue's
service time grows with subscription fanout and catalog churn. Bolt freelist
rewrite cost is persistent and leader-specific, independent of streaming.
"""

TIERS = {
    "small": {"players": 120, "routing_nodes": 24, "churn": 12, "shards": 2, "rps": 20, "bad_nodes": 1},
    "scaled": {"players": 600, "routing_nodes": 96, "churn": 36, "shards": 6, "rps": 60, "bad_nodes": 2},
}


def streaming_delay(config, tier, active):
    if not active or not config["streaming"]:
        return 0.0
    pressure = config["routing_nodes"] * config["churn_per_second"] / config["health_interval"]
    reference = tier["routing_nodes"] * tier["churn"]
    # Reducing nonessential load helps, but cannot cure the residual contention.
    return min(1.2, 0.35 + 0.65 * pressure / reference)


def bolt_delay(free_pages):
    # 8 bytes per free page ID rewritten on every log append.
    return min(0.9, free_pages * 8 / 8_000_000)


def expected_player(player_id):
    return {"id": player_id, "name": f"player-{player_id}", "coins": 1000 + player_id}


def validate_config(changes, current, tier):
    rules = {
        "streaming": (bool, None, None),
        "routing_nodes": (int, 1, tier["routing_nodes"] * 2),
        "churn_per_second": (int, 1, tier["churn"] * 2),
        "health_interval": (int, 1, 60),
        "admission_percent": (int, 0, 100),
        "cache_generation": (int, 1, 10000),
    }
    result = dict(current)
    for key, value in changes.items():
        if key not in rules:
            raise ValueError(f"unknown configuration key: {key}")
        kind, low, high = rules[key]
        if type(value) is not kind or (low is not None and not low <= value <= high):
            raise ValueError(f"invalid value for {key}")
        result[key] = value
    return result
