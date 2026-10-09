"""Private owned SQL link wiring before customer identities and seeding.

Declaring or installing links is not evidence of measured regional fidelity.
Live controls must measure the actual SQL paths and deny control-plane access.
"""

import json
import os
import re
from dataclasses import asdict, replace
from pathlib import Path
from threading import RLock

from sregym.conductor.scenarios.codehub_contracts import LifecyclePhase
from sregym.conductor.scenarios.codehub_links import MAX_LINKS, LinkProfile, LinkSpec, RegionalLinkController
from sregym.service.docker_runtime import trusted_docker_host


def _port(value):
    if type(value) is not int or not 1024 <= value <= 32767:
        raise ValueError("SQL link ports must be explicit nonprivileged integers below the ephemeral range")
    return value


class DatabaseLinkOwner:
    """Remove only the immutable relay captured by its own controller."""

    def __init__(self, relay, specs, baseline, app):
        self.relay, self.specs, self.baseline, self.app = relay, tuple(specs), baseline, app

    def network_factory(self, app, group):
        if app is not self.app or group not in self.app.database_groups or group.name != "group-0":
            raise ValueError("SQL incident links must belong to the captured selected database group")
        writer = next(member for member in group.members if member.role == "writer")
        candidate = next(member for member in group.members if member.role == "candidate")
        directions = {(writer.region, candidate.region), (candidate.region, writer.region)}
        selected = tuple(
            spec
            for spec in self.specs
            if spec.group == group.name and (spec.source_region, spec.target_region) in directions
        )
        if len(selected) != 2:
            raise RuntimeError("The selected regional pair requires both declared SQL directions")
        return DatabaseLinkPartition(self.relay, selected, self.baseline)

    def stop(self):
        self.relay.stop()

    cleanup = stop


class DatabaseLinkPartition:
    """Reset actual established/new SQL connections; restore just the selected pair."""

    def __init__(self, relay, specs, baseline):
        self.relay, self.specs, self.baseline = relay, tuple(specs), baseline
        self._attempted, self._lock = [], RLock()

    def apply(self):
        with self._lock:
            if self._attempted:
                raise RuntimeError("The SQL partition is already active or partially applied")
            try:
                for spec in self.specs:
                    self._attempted.append(spec)
                    self.relay.apply_profile(spec, replace(self.baseline, reset_timeout_ms=0))
            except BaseException as exc:
                try:
                    self.restore()
                except Exception as cleanup_error:
                    exc.add_note(f"Selected SQL link restoration also failed: {cleanup_error}")
                raise

    def restore(self):
        with self._lock:
            failures = []
            for spec in tuple(self._attempted):
                try:
                    self.relay.restore(spec)
                    self._attempted.remove(spec)
                except Exception as exc:
                    failures.append(exc)
            if failures:
                raise ExceptionGroup("Selected SQL links did not all restore their healthy profiles", failures)


class DatabaseNetworkFactory:
    """Pure declaration plus bounded owned SQL wiring, with no fallback path."""

    def __init__(
        self,
        *,
        baseline=LinkProfile(latency_ms=20, jitter_ms=2, bandwidth_kbps=10_000),
        runner_host="172.17.0.1",
        trusted_host=None,
        control_port=18474,
        upstream_base_port=19000,
        data_base_port=21000,
        relay_factory=RegionalLinkController,
        binding_factory=None,
    ):
        if type(baseline) is not LinkProfile or baseline.reset_timeout_ms is not None:
            raise ValueError("Healthy SQL links require a normal profile without connection resets")
        if runner_host != "172.17.0.1":
            raise ValueError("SQL data links require the explicit qualified runner address")
        self.baseline, self.runner_host, self.trusted_host = baseline, runner_host, trusted_host
        self.control_port, self.upstream_base_port, self.data_base_port = map(
            _port, (control_port, upstream_base_port, data_base_port)
        )
        self.relay_factory, self.binding_factory = relay_factory, binding_factory

    def database_links(self, tier):
        """Ordinary deterministic endpoint declaration; no files, sockets or clients."""
        if tier.regions < 2:
            raise ValueError("Regional SQL links require a writer and a separate candidate region")
        regions = tuple(f"region-{chr(97 + index)}" for index in range(tier.regions))
        keys = tuple(
            (source, target, f"group-{group}")
            for group in range(tier.database_groups)
            for source in regions
            for target in regions[:2]
            if source != target
        )
        if not keys or len(keys) > MAX_LINKS:
            raise ValueError("SQL link inventory exceeds its declared bounded capacity")
        ports = {_port(self.data_base_port + index) for index in range(len(keys))}
        upstream = {_port(self.upstream_base_port + index) for index in range(tier.database_groups * 2)}
        if ports & upstream or self.control_port in ports | upstream:
            raise ValueError("Private upstream, data and control listeners must use disjoint ports")
        return {key: self.data_base_port + index for index, key in enumerate(keys)}

    @staticmethod
    def _binding(owner, writer_endpoint, network_factory):
        # The Problem owns this exact private DTO and excludes it from verifier snapshots.
        from sregym.conductor.problems.regional_database_failover import RegionalLinkBinding

        return RegionalLinkBinding(owner, writer_endpoint, network_factory)

    def __call__(self, app, private_dir, start_database_forward):
        if app.inventory.phase != LifecyclePhase.HEALTHY:
            raise RuntimeError("SQL links install after owned bootstrap deployment and before customer seeding")
        declaration = self.database_links(app.tier)
        expected_regions = {f"region-{chr(97 + index)}" for index in range(app.tier.regions)}
        if {region.name for region in app.regions} != expected_regions or {
            group.name for group in app.database_groups
        } != {f"group-{index}" for index in range(app.tier.database_groups)}:
            raise ValueError("Actual SQL region/group inventory differs from its pure declaration")
        targets = {}
        for group in app.database_groups:
            for role, region in (("writer", "region-a"), ("candidate", "region-b")):
                members = [member for member in group.members if member.role == role and member.region == region]
                if len(members) != 1:
                    raise ValueError("Every SQL group needs exactly one declared writer and regional candidate")
                targets[(group.name, region)] = members[0]
        relay = self.relay_factory(
            run_id=app.inventory.run_id,
            trusted_host=self.trusted_host or trusted_docker_host(),
            control_bind="127.0.0.1",
            data_bind=self.runner_host,
            control_port=self.control_port,
        )
        specs = []
        owner = DatabaseLinkOwner(relay, specs, self.baseline, app)
        try:
            upstreams = {}
            for index, key in enumerate(sorted(targets)):
                port = self.upstream_base_port + index
                address = start_database_forward(targets[key], local_port=port, bind_address="127.0.0.1")
                if address != ("127.0.0.1", port):
                    raise RuntimeError("Owned SQL upstream did not use its declared private loopback listener")
                upstreams[key] = address
            for (source, target, group), port in declaration.items():
                host, upstream_port = upstreams[(group, target)]
                specs.append(LinkSpec(source, target, group, port, host, upstream_port))
            owner.specs = tuple(specs)
            relay.prepare(owner.specs, baseline=self.baseline)
            relay.start()
            # Ordinary owned Services/EndpointSlices and actual source/probe SQL are configured here.
            services = app.install_database_links(declaration, runner_host=self.runner_host)
            if type(services) is not dict or set(services) != set(declaration):
                raise RuntimeError("Ordinary SQL link installation returned an incomplete endpoint inventory")
            for key, service in services.items():
                if (
                    type(service) is not str
                    or not re.fullmatch(r"[a-z0-9.-]{1,253}", service)
                    or not service.endswith(".svc.cluster.local")
                ):
                    raise ValueError(f"Installed SQL link endpoint for {key} is not an ordinary service name")
            self._record(private_dir, app.inventory.run_id, owner.specs)
            endpoint = (services[("region-b", "region-a", "group-0")], 3306)
            return (self.binding_factory or self._binding)(owner, endpoint, owner.network_factory)
        except BaseException as exc:
            try:
                owner.stop()
            except Exception as cleanup_error:
                exc.add_note(f"Owned SQL relay cleanup also failed: {cleanup_error}")
            raise

    def _record(self, private_dir, run_id, specs):
        root = Path(private_dir)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise ValueError("SQL link evidence requires the existing owned private run directory")
        path = root / "database-links.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(
                {
                    "version": 1,
                    "run_id": run_id,
                    "baseline": asdict(self.baseline),
                    "links": [asdict(spec) for spec in specs],
                    "regional_fidelity_measured": False,
                },
                output,
                sort_keys=True,
                allow_nan=False,
            )
            output.flush()
            os.fsync(output.fileno())
