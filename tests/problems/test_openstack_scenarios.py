import copy

import pytest
import yaml

from sregym.conductor.oracles.apps.openstack.region_recovery import INVARIANTS
from sregym.conductor.problems.apps.openstack import scenario
from sregym.conductor.problems.apps.openstack.host_access_revocation import revoked_hosts
from sregym.service.apps.openstack.tiers import TIERS


def _spec(name="host_access_revocation"):
    return yaml.safe_load((scenario.SCENARIOS / f"{name}.yaml").read_text())


@pytest.mark.parametrize("path", sorted(scenario.SCENARIOS.glob("*.yaml")), ids=lambda p: p.stem)
def test_every_scenario_is_valid(path):
    loaded = scenario.load(path.stem, known_invariants=set(INVARIANTS))
    for tier in loaded.tiers:
        assert tier in TIERS
        loaded.fault(tier)


def test_fault_rack_exists_in_each_tier():
    loaded = scenario.load("host_access_revocation")
    for tier in loaded.tiers:
        assert loaded.fault(tier)["rack"] in TIERS[tier].racks


def test_missing_sources_or_mapping_is_rejected():
    spec = _spec()
    broken = copy.deepcopy(spec)
    broken["postmortem"]["sources"] = ["http://example.com"]
    del broken["environment"]["deviations"]
    problems = scenario.validate(broken)
    assert "postmortem.sources must be https URLs" in problems
    assert any("deviations" in p for p in problems)


def test_unknown_invariant_and_tier_are_rejected():
    spec = _spec()
    spec["grading"]["invariants"].append("vibes")
    spec["tiers"]["galactic"] = {"rack": "r1"}
    problems = scenario.validate(spec, known_invariants=set(INVARIANTS))
    assert any("unknown invariants" in p for p in problems)
    assert any("unknown tiers" in p for p in problems)


def test_diagnosis_names_the_concrete_rack_and_hosts():
    loaded = scenario.load("host_access_revocation")
    text = loaded.diagnosis(
        rack="r3", hosts="hv-r3-0, hv-r3-1", source="rack-r3-inventory", successor="rack-r3-inventory-v2"
    )
    assert "rack-r3-inventory-v2" in text["description"] and "hv-r3-1" in text["description"]
    assert text["component"] == "host-access-reconciler"


def test_reconciler_log_parsing():
    log = "inventory: 4 hypervisors in 2 racks (r1, r2)\nrevoked broker access: hv-r3-0 (not in inventory)\nrevoked broker access: hv-r3-1 (not in inventory)\n"
    assert revoked_hosts(log) == {"hv-r3-0", "hv-r3-1"}
