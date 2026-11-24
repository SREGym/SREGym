"""Steering decisions of the workspace traffic manager.

These pin the amplifiers the incident depends on: ejection without a cap,
global spillover and anomaly redirection each move a failing region's load onto
the others, while the regional bulkhead and the ejection cap contain it.
"""

import pytest

from sregym.service.apps.workspace.runtime.traffic_manager import DEFAULTS, decide, parse_policy

UNITS = {f"edge-gw-{r}-{i}": r for r in "abc" for i in range(3)}


def health(failed=(), ejected=()):
    return {
        u: {
            "state": "ejected" if u in ejected else "healthy",
            "last_ok": 0 if u in ejected else 1,
            "failed_now": u in failed,
        }
        for u in UNITS
    }


def policy(**changes):
    return {**DEFAULTS, **{k.replace("__", "."): str(v) for k, v in changes.items()}}


def test_policy_text_overrides_defaults_and_rejects_unknown_keys():
    parsed = parse_policy("spillover = global  # loosened\nplacement.chat = alternate\n")
    assert parsed["spillover"] == "global" and parsed["placement.chat"] == "alternate"
    assert parsed["probe"] == "admission"
    with pytest.raises(ValueError, match="unknown setting"):
        parse_policy("session_cache = shared\n")
    with pytest.raises(ValueError, match="spillover"):
        parse_policy("spillover = everywhere\n")


def test_healthy_fleet_serves_each_region_from_its_own_units():
    targets, spilling, _ = decide(UNITS, health(), policy())
    assert targets["a"] == ["edge-gw-a-0", "edge-gw-a-1", "edge-gw-a-2"]
    assert not any(spilling.values())


def test_drained_units_never_serve():
    targets, _, _ = decide(UNITS, health(), policy(drained="edge-gw-b-2"))
    assert "edge-gw-b-2" not in targets["b"]


def test_ejection_cap_keeps_the_most_recently_healthy_units_in_rotation():
    ejected = {"edge-gw-a-0", "edge-gw-a-1", "edge-gw-a-2"}
    _, _, rotation = decide(UNITS, health(ejected=ejected), policy(max_ejection_percent=34))
    assert len(rotation["a"]) == 2
    _, _, rotation = decide(UNITS, health(ejected=ejected), policy(max_ejection_percent=100))
    assert rotation["a"] == []


def test_regional_bulkhead_keeps_a_failing_region_inside_itself():
    ejected = {"edge-gw-a-0", "edge-gw-a-1"}
    targets, spilling, _ = decide(
        UNITS, health(ejected=ejected), policy(spillover="regional", max_ejection_percent=100)
    )
    assert targets["a"] == ["edge-gw-a-2"]
    assert spilling["a"] is None


def test_global_spillover_moves_a_failing_region_onto_every_other_region():
    ejected = {"edge-gw-a-0", "edge-gw-a-1"}
    targets, spilling, _ = decide(
        UNITS, health(ejected=ejected), policy(spillover="global", spillover_below=0.67, max_ejection_percent=100)
    )
    assert set(targets["a"]) == {"edge-gw-a-2"} | {u for u, r in UNITS.items() if r != "a"}
    assert spilling["a"].startswith("spillover")


def test_anomaly_redirect_acts_on_probe_failures_before_any_ejection():
    failed = {"edge-gw-a-0"}
    targets, spilling, _ = decide(
        UNITS, health(failed=failed), policy(anomaly_redirect="enabled", anomaly_threshold=0.3)
    )
    assert spilling["a"].startswith("anomaly")
    assert "edge-gw-b-0" in targets["a"]
    _, spilling, _ = decide(UNITS, health(failed=failed), policy(anomaly_redirect="disabled"))
    assert spilling["a"] is None


def test_a_region_with_nothing_in_rotation_falls_back_to_its_own_units():
    ejected = set(UNITS)
    targets, _, _ = decide(UNITS, health(ejected=ejected), policy(max_ejection_percent=100))
    assert targets["c"] == ["edge-gw-c-0", "edge-gw-c-1", "edge-gw-c-2"]
