"""Owned relay declarations, real-path wiring order, and isolated group faults."""

import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sregym.conductor.scenarios import codehub_network
from sregym.conductor.scenarios.codehub_contracts import LifecyclePhase
from sregym.conductor.scenarios.codehub_links import LinkProfile
from sregym.conductor.scenarios.codehub_network import DatabaseNetworkFactory
from sregym.conductor.scenarios.database_recovery import TIERS


class Relay:
    def __init__(self, events, **options):
        self.events, self.options = events, options
        self.specs = ()
        self.profiles, self.restorations = [], []
        self.stops = 0
        self.fail_apply = self.fail_restore = None

    def prepare(self, specs, *, baseline):
        self.specs, self.baseline = tuple(specs), baseline
        self.events.append(("prepare", self.specs))

    def start(self):
        self.events.append(("start",))

    def apply_profile(self, spec, profile):
        self.profiles.append((spec, profile))
        if self.fail_apply == spec:
            raise OSError("SQL reset response timed out")

    def restore(self, spec):
        self.restorations.append(spec)
        if self.fail_restore == spec:
            raise OSError("SQL restore response timed out")

    def stop(self):
        self.stops += 1
        self.events.append(("stop",))


def wiring(tmp_path, tier_name="small", **options):
    events, relays, installed, forwards = [], [], [], []
    tier = TIERS[tier_name]
    regions = tuple(SimpleNamespace(name=f"region-{chr(97 + index)}") for index in range(tier.regions))
    groups = tuple(
        SimpleNamespace(
            name=f"group-{index}",
            members=tuple(
                SimpleNamespace(name=f"g{index}-{role}", role=role, region=region)
                for role, region in (("writer", "region-a"), ("candidate", "region-b"))
            ),
        )
        for index in range(tier.database_groups)
    )

    def relay_factory(**kwargs):
        relay = Relay(events, **kwargs)
        relays.append(relay)
        return relay

    def install(declaration, *, runner_host):
        events.append(("install",))
        installed.append((dict(declaration), runner_host))
        # This normal helper owns SQL repoint/probe rollout and actual GTID gates.
        return {
            key: f"mysql-link-g{key[2].removeprefix('group-')}-to-{key[1]}.codehub-{key[0]}.svc.cluster.local"
            for key in declaration
        }

    def binding(owner, endpoint, network_factory):
        events.append(("binding",))
        return SimpleNamespace(owner=owner, writer_endpoint=endpoint, network_factory=network_factory)

    app = SimpleNamespace(
        inventory=SimpleNamespace(phase=LifecyclePhase.HEALTHY, run_id=str(uuid4())),
        tier=tier,
        regions=regions,
        database_groups=groups,
        install_database_links=install,
    )
    factory = DatabaseNetworkFactory(
        trusted_host="unix:///owned-trusted.sock", relay_factory=relay_factory, binding_factory=binding, **options
    )

    def forward(member, *, local_port, bind_address):
        events.append(("forward", member.name))
        forwards.append((member.name, local_port, bind_address))
        return bind_address, local_port

    return SimpleNamespace(
        app=app,
        factory=factory,
        forward=forward,
        events=events,
        relays=relays,
        installed=installed,
        forwards=forwards,
        private_dir=tmp_path,
    )


@pytest.mark.parametrize("tier_name,count,upstreams", [("small", 2, 2), ("medium", 4, 4), ("large", 16, 8)])
def test_declaration_is_pure_bounded_and_covers_each_remote_writer(tier_name, count, upstreams, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Pure declaration must not construct a client or inspect the host")

    monkeypatch.setattr(codehub_network, "trusted_docker_host", forbidden)
    factory = DatabaseNetworkFactory(relay_factory=forbidden)
    declared = factory.database_links(TIERS[tier_name])
    assert len(declared) == count
    assert list(declared.values()) == list(range(21000, 21000 + count))
    assert len({(group, target) for source, target, group in declared}) == upstreams
    assert all(source != target and target in {"region-a", "region-b"} for source, target, _group in declared)
    assert all(1024 <= port <= 32767 for port in declared.values())
    assert factory.database_links(TIERS[tier_name]) == declared


@pytest.mark.parametrize(
    "options",
    [
        {"control_port": 21000},
        {"data_base_port": 19000},
        {"data_base_port": 32767},
        {"upstream_base_port": 32767},
        {"control_port": True},
        {"runner_host": "0.0.0.0"},
        {"baseline": LinkProfile(reset_timeout_ms=0)},
    ],
)
def test_invalid_listener_and_healthy_profile_declarations_fail_closed(options):
    with pytest.raises(ValueError):
        DatabaseNetworkFactory(**options).database_links(TIERS["large"])


def test_declaration_rejects_missing_candidate_and_over_capacity_tiers():
    factory = DatabaseNetworkFactory()
    for tier in (replace(TIERS["small"], zones=1), replace(TIERS["large"], zones=26)):
        with pytest.raises(ValueError):
            factory.database_links(tier)


def test_private_upstreams_then_relay_then_real_sql_gate_before_binding(tmp_path):
    state = wiring(tmp_path, "large")
    binding = state.factory(state.app, state.private_dir, state.forward)
    relay = state.relays[0]
    assert [event[0] for event in state.events] == ["forward"] * 8 + ["prepare", "start", "install", "binding"]
    assert len({member for member, _port, _bind in state.forwards}) == 8
    assert [(port, bind) for _member, port, bind in state.forwards] == [
        (19000 + index, "127.0.0.1") for index in range(8)
    ]
    assert len(relay.specs) == 16
    assert relay.options == {
        "run_id": state.app.inventory.run_id,
        "trusted_host": "unix:///owned-trusted.sock",
        "control_bind": "127.0.0.1",
        "data_bind": "172.17.0.1",
        "control_port": 18474,
    }
    assert binding.writer_endpoint == ("mysql-link-g0-to-region-a.codehub-region-b.svc.cluster.local", 3306)
    assert state.installed == [(state.factory.database_links(state.app.tier), "172.17.0.1")]
    public_mapping = state.installed[0][0]
    assert all(type(port) is int for port in public_mapping.values())
    assert 18474 not in public_mapping.values()
    assert not set(public_mapping.values()) & {port for _member, port, _bind in state.forwards}
    evidence = json.loads((tmp_path / "database-links.json").read_text())
    assert evidence["run_id"] == state.app.inventory.run_id
    assert evidence["regional_fidelity_measured"] is False
    assert len(evidence["links"]) == 16
    assert all(link["upstream_host"] == "127.0.0.1" for link in evidence["links"])
    binding.owner.stop()
    assert relay.stops == 1


@pytest.mark.parametrize("mismatch", ["phase", "regions", "groups", "candidate"])
def test_inventory_mismatch_fails_before_owned_mutations(tmp_path, mismatch):
    state = wiring(tmp_path)
    if mismatch == "phase":
        state.app.inventory.phase = LifecyclePhase.PROVISIONING
    elif mismatch == "regions":
        state.app.regions = state.app.regions[:1]
    elif mismatch == "groups":
        state.app.database_groups = ()
    else:
        state.app.database_groups[0].members = state.app.database_groups[0].members[:1]
    with pytest.raises((ValueError, RuntimeError)):
        state.factory(state.app, tmp_path, state.forward)
    assert not state.events and not state.relays and not state.installed


def test_wrong_upstream_listener_never_installs_and_closes_owned_relay(tmp_path):
    state = wiring(tmp_path)
    with pytest.raises(RuntimeError, match="private loopback"):
        state.factory(state.app, tmp_path, lambda *_args, **_kwargs: ("172.17.0.1", 19000))
    assert state.relays[0].stops == 1
    assert not state.installed
    assert not (tmp_path / "database-links.json").exists()


@pytest.mark.parametrize("failure", ["installation", "partial", "admin_endpoint", "evidence_exists", "binding"])
def test_partial_wiring_failure_cleans_only_owned_relay(tmp_path, failure):
    state = wiring(tmp_path)
    if failure == "installation":

        def install(*_args, **_kwargs):
            raise TimeoutError("Actual SQL GTID convergence failed")

        state.app.install_database_links = install
    elif failure == "partial":
        state.app.install_database_links = lambda *_args, **_kwargs: {}
    elif failure == "admin_endpoint":
        state.app.install_database_links = lambda declaration, **_kwargs: {
            key: "127.0.0.1:18474" for key in declaration
        }
    elif failure == "evidence_exists":
        (tmp_path / "database-links.json").write_text("existing captured run")
    else:

        def binding(*_args):
            raise LookupError("Binding construction failed")

        state.factory.binding_factory = binding
    with pytest.raises((TimeoutError, RuntimeError, ValueError, FileExistsError, LookupError)):
        state.factory(state.app, tmp_path, state.forward)
    assert state.relays[0].stops == 1
    assert len(state.forwards) == 2  # Forward handles remain owned by the Problem's failure cleanup.
    if failure == "evidence_exists":
        assert (tmp_path / "database-links.json").read_text() == "existing captured run"


def test_selected_partition_resets_only_pair_and_retains_other_groups(tmp_path):
    state = wiring(tmp_path, "large")
    binding = state.factory(state.app, tmp_path, state.forward)
    relay = state.relays[0]
    partition = binding.network_factory(state.app, state.app.database_groups[0])
    partition.apply()
    assert len(relay.profiles) == 2
    assert {(spec.source_region, spec.target_region, spec.group) for spec, _profile in relay.profiles} == {
        ("region-a", "region-b", "group-0"),
        ("region-b", "region-a", "group-0"),
    }
    assert all(profile == replace(relay.baseline, reset_timeout_ms=0) for _spec, profile in relay.profiles)
    with pytest.raises(RuntimeError, match="already active"):
        partition.apply()
    partition.restore()
    partition.restore()
    assert relay.restorations == [spec for spec, _profile in relay.profiles]
    assert relay.stops == 0
    for app, group in ((SimpleNamespace(), state.app.database_groups[0]), (state.app, state.app.database_groups[1])):
        with pytest.raises(ValueError):
            binding.network_factory(app, group)


def test_partial_reset_restores_every_attempted_link_even_after_response_timeout(tmp_path):
    state = wiring(tmp_path)
    binding = state.factory(state.app, tmp_path, state.forward)
    partition = binding.network_factory(state.app, state.app.database_groups[0])
    relay = state.relays[0]
    relay.fail_apply = partition.specs[1]
    with pytest.raises(OSError, match="reset response timed out"):
        partition.apply()
    assert relay.restorations == list(partition.specs)
    relay.fail_apply = None
    partition.apply()
    relay.fail_restore = partition.specs[0]
    with pytest.raises(ExceptionGroup):
        partition.restore()
    relay.fail_restore = None
    partition.restore()
    assert relay.restorations.count(partition.specs[0]) == 3
    assert relay.restorations.count(partition.specs[1]) == 2


def test_cleanup_failure_keeps_original_wiring_error(tmp_path):
    state = wiring(tmp_path)

    def install(*_args, **_kwargs):
        def fail_stop():
            raise OSError("Owned relay could not be removed")

        state.relays[0].stop = fail_stop
        raise TimeoutError("Actual GTID gate failed")

    state.app.install_database_links = install
    with pytest.raises(TimeoutError, match="Actual GTID gate failed") as captured:
        state.factory(state.app, tmp_path, state.forward)
    assert "Owned SQL relay cleanup also failed" in captured.value.__notes__[0]
