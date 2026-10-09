import base64
import copy
import hashlib
import json
import os
import re
import shlex
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from sregym.conductor.scenarios.codehub_contracts import LifecyclePhase, OwnedResource
from sregym.conductor.scenarios.codehub_regions import REGION_LABEL, ZONE_LABEL, mysql_groups, regional_inventory
from sregym.conductor.scenarios.database_recovery import TIERS, HostCapacity
from sregym.service.apps import codehub as codehub_module
from sregym.service.apps.codehub import MYSQL_BOOTSTRAP_IMAGE, CodeHub


class APIError(Exception):
    def __init__(self, status):
        self.status = status


class RawEndpointSliceResponse:
    def __init__(self, payload, *, status=200, read_error=None):
        self.payload, self.status, self.read_error = payload, status, read_error
        self.read_requests = []
        self.closed = self.released = False

    def read(self, amount):
        self.read_requests.append(amount)
        if self.read_error is not None:
            raise self.read_error
        return self.payload[:amount]

    def close(self):
        self.closed = True

    def release_conn(self):
        self.released = True


class FakeCluster:
    def __init__(self, *, regions=2, groups=1):
        self.core_v1_api = self
        self.namespaces = {}
        self.secrets = {}
        self.deleted = []
        self.commands = []
        self.node_reads = 0
        self.fail_helm_namespace = None
        self.replica_result = "0"
        self.region_count, self.group_count = regions, groups
        self.clone_sources = {}
        self.clone_overrides = {}
        self.bootstrap_override = None
        self.changed_database_uid = False
        self.mysql_image = MYSQL_BOOTSTRAP_IMAGE
        self.clone_error = None
        self.previous_clones = 0
        self.diagnostics = {
            "connection_errors": None,
            "last_sql_error": [{"number": 1396, "message": "Duplicate CREATE USER"}],
            "clone_errors": None,
        }

    def database_resources(self, namespace, name):
        uid = f"database-{namespace}-{name}"
        containers = [{"name": "mysql", "image": self.mysql_image}]
        return [
            {
                "kind": "StatefulSet",
                "metadata": {"name": name, "uid": uid},
                "spec": {"template": {"spec": {"containers": containers}}},
            },
            {
                "kind": "Pod",
                "metadata": {
                    "name": name + "-0",
                    "uid": uid + "-pod",
                    "ownerReferences": [{"kind": "StatefulSet", "uid": uid, "controller": True}],
                },
                "spec": {"containers": containers},
            },
        ]

    @staticmethod
    def database_uuid(namespace, pod):
        return str(UUID(bytes=hashlib.sha256(f"{namespace}/{pod}".encode()).digest()[:16]))

    def list_node(self):
        self.node_reads += 1
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    metadata=SimpleNamespace(
                        name=f"node-{region}-{node}",
                        labels={REGION_LABEL: f"region-{chr(97 + region)}", ZONE_LABEL: f"zone-{region}-{node}"},
                    ),
                    status=SimpleNamespace(conditions=[SimpleNamespace(type="Ready", status="True")]),
                )
                for region in range(self.region_count)
                for node in range(3)
            ]
        )

    def create_namespace(self, body):
        name = body["metadata"]["name"]
        if name in self.namespaces:
            raise APIError(409)
        namespace = SimpleNamespace(
            metadata=SimpleNamespace(name=name, uid="namespace-" + name, labels=body["metadata"]["labels"])
        )
        self.namespaces[name] = namespace
        return namespace

    def read_namespace(self, name, **kwargs):
        if name not in self.namespaces:
            raise APIError(404)
        return self.namespaces[name]

    def delete_namespace(self, name, body):
        assert body["preconditions"]["uid"] == self.namespaces[name].metadata.uid
        self.deleted.append(name)
        del self.namespaces[name]

    def create_namespaced_secret(self, namespace, body):
        name = body["metadata"]["name"]
        self.secrets[(namespace, name)] = body
        return SimpleNamespace(metadata=SimpleNamespace(uid=f"secret-{namespace}-{name}"))

    def exec_command_checked(self, command, input_data=None, timeout=None):
        self.commands.append((command, input_data))
        if command.startswith("helm") and f"-n {self.fail_helm_namespace} " in command:
            raise RuntimeError("Helm deployment failed")
        if " get deployment," in command:
            namespace = command.split()[2]
            roles = (
                ("writer", "reader")
                if namespace.endswith("-a")
                else (("candidate", "reader") if namespace.endswith("-b") else ("reader",))
            )
            return json.dumps(
                {
                    "items": [
                        resource
                        for group in range(self.group_count)
                        for role in roles
                        for resource in self.database_resources(namespace, f"mysql-g{group}-{role}")
                    ]
                }
            )
        if " get statefulset/" in command:
            namespace, name = command.split()[2], command.split()[4].split("/", 1)[1]
            resources = self.database_resources(namespace, name)
            if self.changed_database_uid:
                resources[0]["metadata"]["uid"] = "replaced-controller"
            return json.dumps({"items": resources})
        if "cluster_status" in command:
            namespace = command.split()[2]
            return json.dumps(
                {
                    "running_nodes": [
                        f"rabbit@queue-{index}.queue-headless.{namespace}.svc.cluster.local" for index in range(3)
                    ]
                }
            )
        if input_data and "SELECT @@GLOBAL.gtid_executed" in input_data:
            return "gtid_executed\n38cc05cb-42ee-471a-9f64-6ba1578da0c7:1-6\n"
        if input_data:
            words = command.split()
            namespace, pod = words[2], words[5]
            if "'tables'" in input_data:
                info = {
                    "tables": 0,
                    "channels": 0,
                    "uuid": self.database_uuid(namespace, pod),
                    "read_only": int("writer" not in pod),
                    "version": "8.4.6",
                    "gtid_mode": "ON",
                    "log_bin": 1,
                }
                if self.bootstrap_override:
                    info.update(self.bootstrap_override)
                return json.dumps(info)
            if "'previous'" in input_data:
                return json.dumps({"previous": self.previous_clones})
            if "'gtid',@@GLOBAL.gtid_executed" in input_data:
                return '{"gtid":"38cc05cb-42ee-471a-9f64-6ba1578da0c7:1-6"}'
            if "CLONE INSTANCE FROM" in input_data:
                self.clone_sources[(namespace, pod)] = re.search(r"snapshot_copy'@'([^']+)'", input_data).group(1)
                if self.clone_error:
                    raise RuntimeError(self.clone_error)
            if "'state',STATE" in input_data:
                status = {
                    "state": "Completed",
                    "source": self.clone_sources[(namespace, pod)] + ":3306",
                    "error": 0,
                    "gtid": "38cc05cb-42ee-471a-9f64-6ba1578da0c7:1-6",
                    "covered": 1,
                    "applied": 1,
                    "uuid": self.database_uuid(namespace, pod),
                }
                status.update(self.clone_overrides)
                return json.dumps(status)
            if "'last_sql_error'" in input_data:
                return json.dumps(self.diagnostics)
        if input_data and "WAIT_FOR_EXECUTED_GTID_SET" in input_data:
            return "wait\n" + self.replica_result + "\n"
        return ""


@pytest.fixture
def app(tmp_path, monkeypatch):
    cluster = FakeCluster()
    application = CodeHub(chart_path=tmp_path, kubectl=cluster, lease_path=tmp_path / "campaign.lock")
    application._validate_boundary = lambda: {
        "workload_storage_root": str(tmp_path),
        "trusted_storage_root": str(tmp_path),
    }
    monkeypatch.setattr(HostCapacity, "observe", lambda *_: HostCapacity(64, 256, 1000))
    monkeypatch.setattr("sregym.conductor.scenarios.codehub_capacity.capture_kernel_baseline", lambda _: {})
    return application


def test_constructor_and_unused_cleanup_are_cluster_free(app):
    app.cleanup()
    assert app.inventory.phase == LifecyclePhase.CREATED
    assert app.kubectl.node_reads == 0
    assert app.kubectl.commands == []
    assert app.kubectl.namespaces == {}


def test_capacity_rejects_insufficient_host_before_node_or_namespace_creation(app, monkeypatch):
    monkeypatch.setattr(HostCapacity, "observe", lambda *_: HostCapacity(8, 256, 1000))
    with pytest.raises(ValueError, match="CPU headroom"):
        app.deploy()
    assert not app.kubectl.node_reads and not app.kubectl.namespaces
    assert app.inventory.phase == LifecyclePhase.CREATED and app._environment_lease is None


def test_application_lease_prevents_second_deployment_until_owned_cleanup(app, monkeypatch):
    app.deploy()
    competing = CodeHub(chart_path=app.chart_path, kubectl=FakeCluster(), lease_path=app.lease_path)
    competing._validate_boundary = app._validate_boundary
    with pytest.raises(RuntimeError, match="already owns"):
        competing.deploy()
    assert not competing.kubectl.node_reads and not competing.kubectl.namespaces
    state = app.__getstate__()
    assert state["_environment_lease"] is None and app._environment_lease.handle is not None
    app.cleanup()
    competing.deploy()
    competing.cleanup()


def test_deployment_requires_real_complete_regions_and_checks_durable_dependencies(app):
    app.deploy()
    assert app.inventory.phase == LifecyclePhase.HEALTHY
    assert len(app.regions) == 2
    assert len(app.database_groups[0].members) == 4
    commands = app.kubectl.commands
    assert any("WAIT_FOR_EXECUTED_GTID_SET" in (data or "") for _, data in commands)
    assert sum("cluster_status" in command for command, _ in commands) == 6
    assert any("codehub.migrate" in command for command, _ in commands)
    assert all("SOURCE_PASSWORD=" not in command for command, _ in commands)
    app.cleanup()
    assert app.inventory.phase == LifecyclePhase.STOPPED
    assert not app.inventory.cleanup_pending
    assert app.kubectl.namespaces == {}
    app.cleanup()
    assert len(app.kubectl.deleted) == 2


def test_partial_deployment_cleanup_removes_only_uid_owned_namespaces(app):
    app.kubectl.fail_helm_namespace = "codehub-region-b"
    with pytest.raises(RuntimeError, match="Helm"):
        app.deploy()
    assert app.inventory.phase == LifecyclePhase.STOPPED
    assert set(app.kubectl.deleted) == {"codehub-region-a", "codehub-region-b"}
    assert app.kubectl.namespaces == {}


def test_existing_namespace_is_never_adopted_or_deleted(app):
    external = SimpleNamespace(metadata=SimpleNamespace(uid="external", labels={}))
    app.kubectl.namespaces["codehub-region-a"] = external
    with pytest.raises(APIError):
        app.deploy()
    assert app.kubectl.namespaces["codehub-region-a"] is external
    assert app.kubectl.deleted == []


def test_changed_namespace_uid_is_not_deleted(app):
    app.deploy()
    app.kubectl.namespaces["codehub-region-b"].metadata.uid = "replacement"
    with pytest.raises(RuntimeError, match="ownership changed"):
        app.cleanup()
    assert app.kubectl.deleted == ["codehub-region-a"]
    assert "codehub-region-b" in app.kubectl.namespaces
    assert app.inventory.phase == LifecyclePhase.STOPPING


def test_failed_replica_convergence_prevents_healthy_admission_and_cleans_up(app):
    app.kubectl.replica_result = "1"
    with pytest.raises(RuntimeError, match="did not converge"):
        app.deploy()
    assert app.inventory.phase == LifecyclePhase.STOPPED
    assert not app.kubectl.namespaces


def test_gateway_tls_has_region_identity_and_captured_client_ca(app):
    from cryptography import x509

    app.deploy()
    secret = app.kubectl.secrets[("codehub-region-a", "gateway-tls")]
    certificate = x509.load_pem_x509_certificate(base64.b64decode(secret["data"]["tls.crt"]))
    authority = x509.load_pem_x509_certificate(app.gateway_certificates["codehub-region-a"].encode())
    assert certificate.issuer == authority.subject
    names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "gateway.codehub-region-a.svc.cluster.local" in names.get_values_for_type(x509.DNSName)


def test_forwarding_targets_only_declared_regions_and_roles(app):
    nodes = {
        f"node-{region}-{node}": {REGION_LABEL: f"region-{chr(97 + region)}", ZONE_LABEL: f"zone-{region}-{node}"}
        for region in range(2)
        for node in range(3)
    }
    app.regions = regional_inventory(TIERS["small"], nodes)
    app.database_groups = mysql_groups(TIERS["small"], app.regions)
    command = app.port_forward_command("region-b", "gateway", 18080, tls=True)
    assert "18080:8443" in command and "codehub-region-b" in command
    with pytest.raises(ValueError):
        app.port_forward_command("region-c", "gateway", 18080)
    with pytest.raises(ValueError):
        app.port_forward_command("region-b", "topology", 18080, tls=True)


def test_standalone_brokers_are_not_admitted_as_a_three_member_cluster(app, monkeypatch):
    nodes = {
        f"node-{region}-{node}": {REGION_LABEL: f"region-{chr(97 + region)}", ZONE_LABEL: f"zone-{region}-{node}"}
        for region in range(2)
        for node in range(3)
    }
    app.regions = regional_inventory(TIERS["small"], nodes)
    app.kubectl.exec_command_checked = lambda *_a, **_kw: '{"running_nodes": ["rabbit@only-node"]}'
    clock = iter((0, 1000))
    monkeypatch.setattr(codehub_module.time, "monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match="complete three-node"):
        app._await_queue_membership()


class RoutingCluster(FakeCluster):
    def __init__(self):
        super().__init__()
        self.maps = {}
        self.services = {}
        self.patches = []
        self.patch_attempts = 0
        self.fail_attempt = None
        self.concurrent_change = False

    def read_namespaced_config_map(self, name, namespace, **kwargs):
        return copy.deepcopy(self.maps[(namespace, name)])

    def patch_namespaced_config_map(self, name, namespace, body, **kwargs):
        self.patch_attempts += 1
        if self.patch_attempts == self.fail_attempt:
            if self.concurrent_change:
                first = self.maps[next(iter(self.maps))]
                first.data["other.json"] = "concurrent customer configuration"
                first.metadata.resource_version = str(int(first.metadata.resource_version) + 1)
            raise APIError(409)
        current = self.maps[(namespace, name)]
        assert body["metadata"]["uid"] == current.metadata.uid
        if body["metadata"]["resourceVersion"] != current.metadata.resource_version:
            raise APIError(409)
        current.data = copy.deepcopy(body["data"])
        current.metadata.resource_version = str(int(current.metadata.resource_version) + 1)
        self.patches.append((namespace, name, copy.deepcopy(body)))
        return copy.deepcopy(current)

    def read_namespaced_service(self, name, namespace, **kwargs):
        return copy.deepcopy(self.services[(namespace, name)])

    def patch_namespaced_service(self, name, namespace, body, **kwargs):
        current = self.services[(namespace, name)]
        assert body["metadata"]["uid"] == current.metadata.uid
        assert body["metadata"]["resourceVersion"] == current.metadata.resource_version
        current.spec.external_name = body["spec"]["externalName"]
        current.metadata.resource_version = str(int(current.metadata.resource_version) + 1)
        self.patches.append((namespace, name, copy.deepcopy(body)))


@pytest.fixture
def routing_app(tmp_path):
    cluster = RoutingCluster()
    application = CodeHub(tier=TIERS["medium"], chart_path=tmp_path, kubectl=cluster)
    nodes = {
        f"node-{region}-{node}": {REGION_LABEL: f"region-{chr(97 + region)}", ZONE_LABEL: f"zone-{region}-{node}"}
        for region in range(2)
        for node in range(3)
    }
    application.regions = regional_inventory(application.tier, nodes)
    application.database_groups = mysql_groups(application.tier, application.regions)
    existing = str(uuid4())
    for region in application.regions:
        application._create_namespace(region.namespace)
        for name in ("database-routes", "worker-database-routes"):
            uid = f"{region.namespace}-{name}"
            cluster.maps[(region.namespace, name)] = SimpleNamespace(
                metadata=SimpleNamespace(uid=uid, resource_version="1"),
                data={
                    "database-routes.json": json.dumps(
                        {
                            "*": application._normal_group_route(region, application.database_groups[0]),
                            existing: {"group": "untouched", "writer_host": "customer.example"},
                        }
                    ),
                    "other.json": "ordinary customer configuration",
                },
            )
            application._record("ConfigMap", region.namespace, name, uid)
        for name in ("mysql-writer", "mysql-g0-writer-route", "mysql-g1-writer-route"):
            uid = f"{region.namespace}-{name}"
            cluster.services[(region.namespace, name)] = SimpleNamespace(
                metadata=SimpleNamespace(uid=uid, resource_version="1"),
                spec=SimpleNamespace(external_name="original.example"),
            )
            application._record("Service", region.namespace, name, uid)
    return application, existing


def test_tenant_routes_cover_each_group_in_both_maps_and_preserve_other_customers(routing_app):
    app, existing = routing_app
    tenant = str(uuid4())
    app.configure_tenant_route(tenant, app.database_groups[1].name)
    assert len(app.kubectl.patches) == 4
    for region in app.regions:
        for name in ("database-routes", "worker-database-routes"):
            current = app.kubectl.maps[(region.namespace, name)]
            routes = json.loads(current.data["database-routes.json"])
            assert routes[existing] == {"group": "untouched", "writer_host": "customer.example"}
            assert routes[tenant]["writer_host"] == f"mysql-g1-writer-route.{region.namespace}.svc.cluster.local"
            assert routes[tenant]["reader_host"].startswith(f"mysql-g1-reader.{region.namespace}.")
            assert routes[tenant]["read_port"] == 3306
            assert current.data["other.json"] == "ordinary customer configuration"


@pytest.mark.parametrize("change", ["namespace", "configmap", "assignment", "uncaptured"])
def test_invalid_route_ownership_or_reassignment_is_rejected_before_any_update(routing_app, change):
    app, existing = routing_app
    namespace = app.regions[-1].namespace
    if change == "namespace":
        app.kubectl.namespaces[namespace].metadata.uid = "replacement"
    elif change == "configmap":
        app.kubectl.maps[(namespace, "worker-database-routes")].metadata.uid = "replacement"
    elif change == "uncaptured":
        app.inventory = replace(
            app.inventory, resources=tuple(r for r in app.inventory.resources if r.kind != "ConfigMap")
        )
    with pytest.raises((RuntimeError, ValueError)):
        app.configure_tenant_route(existing if change == "assignment" else str(uuid4()), app.database_groups[1].name)
    assert app.kubectl.patches == []


def test_partial_routing_update_rolls_back_only_the_versions_it_changed(routing_app):
    app, _ = routing_app
    before = copy.deepcopy(app.kubectl.maps)
    app.kubectl.fail_attempt = 3
    with pytest.raises(APIError):
        app.configure_tenant_route(str(uuid4()), app.database_groups[1].name)
    assert len(app.kubectl.patches) == 4
    assert all(current.data == before[key].data for key, current in app.kubectl.maps.items())


def test_routing_rollback_preserves_a_concurrent_customer_change(routing_app):
    app, _ = routing_app
    app.kubectl.fail_attempt = 2
    app.kubectl.concurrent_change = True
    with pytest.raises(ExceptionGroup, match="guarded rollback"):
        app.configure_tenant_route(str(uuid4()), app.database_groups[1].name)
    first = next(iter(app.kubectl.maps.values()))
    assert first.data["other.json"] == "concurrent customer configuration"
    assert len(app.kubectl.patches) == 1


@pytest.mark.parametrize(
    "group_index,expected", [(0, {"mysql-writer", "mysql-g0-writer-route"}), (1, {"mysql-g1-writer-route"})]
)
def test_writer_route_changes_only_the_selected_group_and_group_zero_compatibility(routing_app, group_index, expected):
    app, _ = routing_app
    member = next(m for m in app.database_groups[group_index].members if m.role == "candidate")
    app.set_writer_route("region-b", member)
    assert {name for _, name, _ in app.kubectl.patches} == expected
    assert all(namespace == "codehub-region-b" for namespace, _, _ in app.kubectl.patches)
    assert all(
        body["spec"]["externalName"] == member.origin.removeprefix("mysql://").rsplit(":", 1)[0]
        for _, _, body in app.kubectl.patches
    )


def mock_route_inventory(app, monkeypatch):
    identities = {}

    def inventory(region, role, **_kwargs):
        return tuple(
            (name, identities.setdefault((region.namespace, name), f"{region.namespace}/{name}"))
            for name in (f"{role}-0", f"{role}-1")
        )

    monkeypatch.setattr(app, "_serving_route_inventory", inventory)
    monkeypatch.setattr(
        app.kubectl,
        "read_namespaced_pod",
        lambda name, namespace, **_kwargs: SimpleNamespace(metadata=SimpleNamespace(uid=identities[(namespace, name)])),
        raising=False,
    )
    return identities


def mounted_route_digest(app, command):
    arguments = shlex.split(command)
    assert arguments[:2] == ["exec", "kubectl"]
    namespace, pod = arguments[3], arguments[5]
    name = "worker-database-routes" if pod.startswith("worker") else "database-routes"
    return hashlib.sha256(app.kubectl.maps[(namespace, name)].data["database-routes.json"].encode()).hexdigest()


def test_route_projection_requires_every_serving_replica_and_the_actual_file_bytes(routing_app, monkeypatch):
    app, _ = routing_app
    tenant = str(uuid4())
    group = app.database_groups[1].name
    app.configure_tenant_route(tenant, group)
    seen = []
    reads = 0
    mock_route_inventory(app, monkeypatch)
    monkeypatch.setattr(codehub_module.time, "sleep", lambda seconds: None)

    def mounted(command, *, timeout):
        nonlocal reads
        reads += 1
        assert 0 < timeout <= 5
        arguments = shlex.split(command)
        namespace, pod = arguments[3], arguments[5]
        seen.append((namespace, pod))
        digest = mounted_route_digest(app, command)
        return "older-configmap-projection" if reads == 12 else digest

    app.kubectl.exec_command_checked = mounted
    app.await_database_routes({tenant: group}, timeout_seconds=2)
    assert reads == 24
    assert len(set(seen)) == 12


def test_route_projection_fails_closed_when_declared_customer_mapping_is_missing(routing_app, monkeypatch):
    app, _ = routing_app
    monkeypatch.setattr(
        app, "_serving_route_inventory", lambda *_a, **_kw: pytest.fail("Missing routing must fail before pod access")
    )
    with pytest.raises(RuntimeError, match="omits declared"):
        app.await_database_routes({str(uuid4()): app.database_groups[1].name})


def test_route_projection_timeout_never_accepts_a_stale_file(routing_app, monkeypatch):
    app, _ = routing_app
    tenant = str(uuid4())
    group = app.database_groups[1].name
    app.configure_tenant_route(tenant, group)
    mock_route_inventory(app, monkeypatch)
    monkeypatch.setattr(app.kubectl, "exec_command_checked", lambda *a, **kw: "stale")
    now = [0]
    monkeypatch.setattr(codehub_module.time, "sleep", lambda _: now.__setitem__(0, 2))
    monkeypatch.setattr(codehub_module.time, "monotonic", lambda: now[0])
    with pytest.raises(TimeoutError, match="every serving"):
        app.await_database_routes({tenant: group}, timeout_seconds=1)


@pytest.mark.parametrize("unready_at", [1, 7])
def test_route_projection_waits_for_initial_and_recheck_ready_cohorts(routing_app, monkeypatch, unready_at):
    app, _ = routing_app
    tenant, group = str(uuid4()), app.database_groups[1].name
    app.configure_tenant_route(tenant, group)
    mock_route_inventory(app, monkeypatch)
    original = app._serving_route_inventory
    observations, checks = 0, []

    def inventory(*args, **kwargs):
        nonlocal observations
        observations += 1
        if observations == unready_at:
            raise codehub_module.RouteReplicasUnavailable("Owned cohort is temporarily incomplete")
        return original(*args, **kwargs)

    def mounted(command, **kwargs):
        checks.append(command)
        return mounted_route_digest(app, command)

    monkeypatch.setattr(app, "_serving_route_inventory", inventory)
    monkeypatch.setattr(codehub_module.time, "sleep", lambda _: None)
    app.kubectl.exec_command_checked = mounted
    app.await_database_routes({tenant: group}, timeout_seconds=2)
    assert observations > unready_at
    assert len(checks) == (12 if unready_at == 1 else 24)


def test_route_projection_never_accepts_a_persistently_incomplete_cohort(routing_app, monkeypatch):
    app, _ = routing_app
    tenant, group = str(uuid4()), app.database_groups[1].name
    app.configure_tenant_route(tenant, group)

    def incomplete(*args, **kwargs):
        raise codehub_module.RouteReplicasUnavailable("Owned cohort is incomplete")

    monkeypatch.setattr(app, "_serving_route_inventory", incomplete)
    monkeypatch.setattr(
        app.kubectl, "exec_command_checked", lambda *_a, **_kw: pytest.fail("Incomplete cohort was used")
    )
    now = [0]
    monkeypatch.setattr(codehub_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(codehub_module.time, "sleep", lambda _: now.__setitem__(0, 2))
    with pytest.raises(TimeoutError, match="every serving"):
        app.await_database_routes({tenant: group}, timeout_seconds=1)


def test_route_projection_wait_does_not_retry_ownership_failures(routing_app, monkeypatch):
    app, _ = routing_app
    tenant, group = str(uuid4()), app.database_groups[1].name
    app.configure_tenant_route(tenant, group)

    def foreign(*args, **kwargs):
        raise RuntimeError("Serving route pod differs from its captured controller")

    monkeypatch.setattr(app, "_serving_route_inventory", foreign)
    monkeypatch.setattr(codehub_module.time, "sleep", lambda _: pytest.fail("Foreign ownership must fail immediately"))
    with pytest.raises(RuntimeError, match="captured controller"):
        app.await_database_routes({tenant: group}, timeout_seconds=1)


def test_route_projection_cancels_while_waiting_for_ready_replicas(routing_app, monkeypatch):
    app, _ = routing_app
    tenant, group = str(uuid4()), app.database_groups[1].name
    app.configure_tenant_route(tenant, group)
    cancel, waits = threading.Event(), []

    def incomplete(*args, **kwargs):
        raise codehub_module.RouteReplicasUnavailable("Owned cohort is incomplete")

    def wait(seconds):
        waits.append(seconds)
        cancel.set()

    monkeypatch.setattr(app, "_serving_route_inventory", incomplete)
    monkeypatch.setattr(cancel, "wait", wait)
    with pytest.raises(RuntimeError, match="cancelled"):
        app.await_database_routes({tenant: group}, timeout_seconds=1, cancel=cancel)
    assert len(waits) == 1 and 0 < waits[0] <= 0.2


def test_route_projection_checks_replicas_concurrently_with_a_fixed_bound(routing_app, monkeypatch):
    app, _ = routing_app
    tenant, group = str(uuid4()), app.database_groups[1].name
    app.configure_tenant_route(tenant, group)
    mock_route_inventory(app, monkeypatch)
    gate, lock = threading.Barrier(8), threading.Lock()
    calls, active, peak = 0, 0, 0

    def mounted(command, *, timeout):
        nonlocal calls, active, peak
        assert 0 < timeout <= 5
        with lock:
            calls += 1
            number = calls
            active += 1
            peak = max(peak, active)
        try:
            if number <= 8:
                gate.wait(timeout=5)
            return mounted_route_digest(app, command)
        finally:
            with lock:
                active -= 1

    app.kubectl.exec_command_checked = mounted
    app.await_database_routes({tenant: group})
    assert calls == 12 and peak == 8 and active == 0


@pytest.mark.parametrize("change", ["map_version", "pod_uid"])
def test_route_projection_rechecks_a_changed_map_or_serving_incarnation(routing_app, monkeypatch, change):
    app, _ = routing_app
    tenant, group = str(uuid4()), app.database_groups[1].name
    app.configure_tenant_route(tenant, group)
    identities = mock_route_inventory(app, monkeypatch)
    namespace = app.regions[0].namespace
    calls, changed = 0, False
    lock = threading.Lock()

    def mounted(command, **_kwargs):
        nonlocal calls, changed
        with lock:
            calls += 1
            if not changed and shlex.split(command)[3:6:2] == [namespace, "api-0"]:
                changed = True
                if change == "map_version":
                    current = app.kubectl.maps[(namespace, "database-routes")]
                    current.metadata.resource_version = str(int(current.metadata.resource_version) + 1)
                else:
                    identities[(namespace, "api-0")] = "same-name-new-pod"
        return mounted_route_digest(app, command)

    app.kubectl.exec_command_checked = mounted
    app.await_database_routes({tenant: group})
    assert changed and calls == 24


def test_route_projection_cancellation_drains_active_and_queued_checks(routing_app, monkeypatch):
    app, _ = routing_app
    tenant, group = str(uuid4()), app.database_groups[1].name
    app.configure_tenant_route(tenant, group)
    mock_route_inventory(app, monkeypatch)
    cancel, entered, release = threading.Event(), threading.Event(), threading.Event()
    lock = threading.Lock()
    active, calls, errors = 0, 0, []

    def mounted(command, **_kwargs):
        nonlocal active, calls
        with lock:
            active += 1
            calls += 1
        entered.set()
        try:
            assert release.wait(3)
            return mounted_route_digest(app, command)
        finally:
            with lock:
                active -= 1

    def run():
        try:
            app.await_database_routes({tenant: group}, cancel=cancel)
        except RuntimeError as error:
            errors.append(str(error))

    app.kubectl.exec_command_checked = mounted
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(3)
        cancel.set()
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive() and active == 0 and calls <= 8
    assert len(errors) == 1 and "cancelled" in errors[0]


def test_route_projection_cancellation_bounds_each_inventory_request(routing_app, monkeypatch):
    app, _ = routing_app
    tenant, group = str(uuid4()), app.database_groups[1].name
    app.configure_tenant_route(tenant, group)
    cancel, timeouts = threading.Event(), []
    original = app.kubectl.read_namespace

    def namespace(name, **kwargs):
        timeouts.append(kwargs["_request_timeout"])
        cancel.set()
        return original(name, **kwargs)

    monkeypatch.setattr(app.kubectl, "read_namespace", namespace)
    monkeypatch.setattr(
        app.kubectl,
        "read_namespaced_config_map",
        lambda *_a, **_kw: pytest.fail("Cancelled inventory made another request"),
    )
    with pytest.raises(RuntimeError, match="cancelled"):
        app.await_database_routes({tenant: group}, timeout_seconds=1, cancel=cancel)
    assert len(timeouts) == 1 and isinstance(timeouts[0], tuple)
    assert all(0 < value <= 1 for value in timeouts[0]) and sum(timeouts[0]) <= 1


def test_fractional_route_timeout_reaches_the_real_kubernetes_rest_client(monkeypatch):
    import urllib3
    from kubernetes.client import Configuration
    from kubernetes.client.rest import RESTClientObject

    rest, timeouts = RESTClientObject(Configuration()), []

    def request(*_args, **kwargs):
        timeouts.append(kwargs["timeout"])
        return urllib3.HTTPResponse(status=200, body=b"{}")

    monkeypatch.setattr(rest.pool_manager, "request", request)
    try:
        rest.GET("https://unused.invalid", _preload_content=False, _request_timeout=(0.05, 0.05))
    finally:
        rest.pool_manager.clear()
    assert len(timeouts) == 1
    assert timeouts[0].connect_timeout == 0.05 and timeouts[0].read_timeout == 0.05


def test_route_observation_disables_retry_after_without_changing_the_shared_client(routing_app, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from kubernetes.client import ApiClient, Configuration, CoreV1Api
    from kubernetes.client.rest import ApiException

    app, _ = routing_app
    tenant, group = str(uuid4()), app.database_groups[1].name
    app.configure_tenant_route(tenant, group)
    requests = []

    class Busy(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            body = b'{"reason":"ServiceUnavailable"}'
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Retry-After", "2")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Busy)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    configuration = Configuration()
    configuration.host = f"http://127.0.0.1:{server.server_port}"
    configuration.retries = 3
    try:
        with ApiClient(configuration) as shared:
            monkeypatch.setattr(app, "_client", lambda: SimpleNamespace(core_v1_api=CoreV1Api(shared)))
            started = time.monotonic()
            with pytest.raises(ApiException) as error:
                app.await_database_routes({tenant: group}, timeout_seconds=1)
            assert error.value.status == 503 and time.monotonic() - started < 1.5
            assert shared.configuration.retries == 3 and len(requests) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert not thread.is_alive()


def test_worker_projection_requires_every_worker_final_bytes_and_preserves_other_routes(routing_app, monkeypatch):
    app, _ = routing_app
    region, group = app.regions[-1], app.database_groups[1].name
    tenant = str(uuid4())
    app.configure_tenant_route(tenant, group)
    current = app.kubectl.maps[(region.namespace, "worker-database-routes")]
    routes = json.loads(current.data["database-routes.json"])
    routes[tenant].update(writer_host="mysql-g1-recovered", port=3306)
    current.data["database-routes.json"] = json.dumps(routes)
    mock_route_inventory(app, monkeypatch)
    checks = []

    def mounted(command, **_kwargs):
        arguments = shlex.split(command)
        checks.append((arguments[3], arguments[5]))
        assert arguments[3] == region.namespace and arguments[5].startswith("worker-")
        return "stale" if len(checks) == 2 else mounted_route_digest(app, command)

    app.kubectl.exec_command_checked = mounted
    app.await_worker_group_route(region.name, group, "mysql-g1-recovered", 3306)
    assert len(checks) == 4 and len(set(checks)) == 2
    assert current.data["database-routes.json"] == json.dumps(routes)


@pytest.mark.skipif(os.name != "posix", reason="Route command replaces its POSIX shell")
def test_route_command_timeout_reaps_the_replacement_process(tmp_path):
    from sregym.service.kubectl import KubeCtl

    pidfile = tmp_path / "route-command.pid"
    program = f"import os,pathlib,time;pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()));time.sleep(60)"
    command = f"exec {shlex.quote(sys.executable)} -c {shlex.quote(program)}"
    with pytest.raises(RuntimeError, match="timed out"):
        KubeCtl.__new__(KubeCtl).exec_command_checked(command, timeout=0.5)
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("ready", [True, False])
def test_serving_route_pods_require_the_captured_replica_owner(routing_app, monkeypatch, ready):
    app, _ = routing_app
    region = app.regions[0]
    uid = "captured-api-deployment"
    app.inventory = app.inventory.with_resource(
        OwnedResource(app.inventory.run_id, "Deployment", region.namespace, "api", uid)
    )
    pod = SimpleNamespace(
        metadata=SimpleNamespace(
            name="api-current",
            uid="api-current-uid",
            deletion_timestamp=None,
            owner_references=[
                SimpleNamespace(kind="ReplicaSet", name="api-current-rs", uid="current-rs", controller=True)
            ],
        ),
        status=SimpleNamespace(conditions=[SimpleNamespace(type="Ready", status="True" if ready else "False")]),
    )
    replica = SimpleNamespace(
        metadata=SimpleNamespace(
            uid="current-rs",
            owner_references=[
                SimpleNamespace(kind="Deployment", name="api", uid="replacement-deployment", controller=True)
            ],
        )
    )
    app.kubectl.api_client = object()
    app.kubectl.list_namespaced_pod = lambda *a, **kw: SimpleNamespace(items=[pod])
    client = SimpleNamespace(AppsV1Api=lambda _: SimpleNamespace(read_namespaced_replica_set=lambda *a, **kw: replica))
    monkeypatch.setitem(sys.modules, "kubernetes", SimpleNamespace(client=client))
    monkeypatch.setitem(sys.modules, "kubernetes.client", client)
    with pytest.raises(RuntimeError, match="captured controller"):
        app._serving_route_pods(region, "api")


def declared_links(tier):
    keys = [
        (source, target, f"group-{group}")
        for group in range(tier.database_groups)
        for source in (f"region-{chr(97 + i)}" for i in range(tier.regions))
        for target in ("region-a", "region-b")
        if source != target
    ]
    return {key: 21000 + i for i, key in enumerate(keys)}


@pytest.mark.parametrize(
    "invalid",
    [
        {("region-b", "region-a", "group-0"): 21000},
        {("region-a", "region-b", "group-0"): 21000, ("region-b", "region-a", "group-0"): 21000},
        {("region-a", "region-b", "group-0"): 65535, ("region-b", "region-a", "group-0"): 21001},
    ],
)
def test_database_connection_declaration_rejects_omissions_duplicate_or_unbounded_ports(invalid):
    with pytest.raises(ValueError):
        CodeHub(database_links=invalid)


@pytest.fixture
def linked_app(routing_app, monkeypatch):
    app, existing = routing_app
    app.database_links = declared_links(app.tier)
    app.inventory = replace(app.inventory, phase=LifecyclePhase.HEALTHY)
    cluster = app.kubectl
    cluster.api_client = object()
    cluster.slices, cluster.policies, cluster.deployments = {}, {}, {}
    cluster.slice_patches, cluster.deployment_patches, cluster.sql_calls, cluster.events = [], [], [], []
    cluster.slice_payloads, cluster.slice_reads, cluster.slice_responses = {}, [], []
    cluster.slice_status, cluster.slice_read_error = 200, None

    def capture(kind, namespace, name):
        uid = f"{namespace}-{kind}-{name}"
        resource = OwnedResource(app.inventory.run_id, kind, namespace, name, uid)
        app.inventory = replace(app.inventory, resources=app.inventory.resources + (resource,))
        return SimpleNamespace(uid=uid, resource_version="1")

    for region in app.regions:
        ports = [port for (source, _, _), port in app.database_links.items() if source == region.name]
        cluster.policies[region.namespace] = SimpleNamespace(
            metadata=capture("NetworkPolicy", region.namespace, "database-link-egress"),
            spec=SimpleNamespace(
                egress=[
                    SimpleNamespace(
                        to=[SimpleNamespace(ip_block=SimpleNamespace(cidr="172.17.0.1/32", _except=[]))],
                        ports=[SimpleNamespace(port=port, protocol="TCP") for port in ports],
                    )
                ]
            ),
        )
        cluster.maps[(region.namespace, "topology-config")] = SimpleNamespace(
            metadata=capture("ConfigMap", region.namespace, "topology-config"),
            data={
                "topology.json": json.dumps(
                    {
                        "writer_host": "original.example",
                        "candidate_host": "candidate.example",
                        "routing_service": "mysql-g0-writer-route",
                        "automatic_promotion": region.name == "region-b",
                        "failure_threshold": 3,
                    }
                ),
                "other.txt": "unchanged",
            },
        )
        cluster.deployments[region.namespace] = SimpleNamespace(
            metadata=capture("Deployment", region.namespace, "topology")
        )
        for (source, target, group), port in app.database_links.items():
            if source != region.name:
                continue
            name = app._database_link_name(target, group)
            cluster.services[(region.namespace, name)] = SimpleNamespace(
                metadata=capture("Service", region.namespace, name),
                spec=SimpleNamespace(type="ClusterIP", selector=None, ports=[SimpleNamespace(port=3306)]),
            )
            metadata = capture("EndpointSlice", region.namespace, name)
            metadata.labels = {"kubernetes.io/service-name": name}
            cluster.slices[(region.namespace, name)] = SimpleNamespace(
                metadata=metadata,
                address_type="IPv4",
                endpoints=[],
                ports=[{"name": "mysql", "protocol": "TCP", "port": port}],
            )

    def slice_payload(namespace, name):
        current = cluster.slices[(namespace, name)]
        payload = {
            "apiVersion": "discovery.k8s.io/v1",
            "kind": "EndpointSlice",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "uid": current.metadata.uid,
                "resourceVersion": current.metadata.resource_version,
                "labels": current.metadata.labels,
            },
            "addressType": current.address_type,
            "ports": current.ports,
        }
        # Match the API server's omission of an empty list on the wire.
        if current.endpoints:
            payload["endpoints"] = current.endpoints
        return copy.deepcopy(payload)

    def read_slice(name, namespace, **kwargs):
        if kwargs.get("_preload_content", True):
            raise ValueError("Invalid value for endpoints must not be None")
        cluster.slice_reads.append((namespace, name, kwargs))
        cluster.events.append("slice-read")
        payload = cluster.slice_payloads.get((namespace, name), slice_payload(namespace, name))
        response = RawEndpointSliceResponse(
            json.dumps(payload).encode() if type(payload) is dict else payload,
            status=cluster.slice_status,
            read_error=cluster.slice_read_error,
        )
        cluster.slice_responses.append(response)
        return response

    cluster.slice_payload = slice_payload

    def patch_slice(name, namespace, body, **kwargs):
        current = cluster.slices[(namespace, name)]
        assert body["metadata"]["uid"] == current.metadata.uid
        assert body["metadata"]["resourceVersion"] == current.metadata.resource_version
        cluster.slice_patches.append((namespace, name, body))
        cluster.events.append("slice")
        current.endpoints = body["endpoints"]

    def patch_deployment(name, namespace, body, **kwargs):
        current = cluster.deployments[namespace]
        assert body["metadata"]["uid"] == current.metadata.uid
        assert body["metadata"]["resourceVersion"] == current.metadata.resource_version
        cluster.deployment_patches.append((namespace, body))
        cluster.events.append("rollout")

    clients = SimpleNamespace(
        DiscoveryV1Api=lambda _: SimpleNamespace(
            read_namespaced_endpoint_slice=read_slice,
            patch_namespaced_endpoint_slice=patch_slice,
        ),
        NetworkingV1Api=lambda _: SimpleNamespace(
            read_namespaced_network_policy=lambda name, namespace, **kw: copy.deepcopy(cluster.policies[namespace])
        ),
        AppsV1Api=lambda _: SimpleNamespace(
            read_namespaced_deployment=lambda name, namespace, **kw: copy.deepcopy(cluster.deployments[namespace]),
            patch_namespaced_deployment=patch_deployment,
        ),
    )
    monkeypatch.setitem(sys.modules, "kubernetes", SimpleNamespace(client=clients))
    monkeypatch.setitem(sys.modules, "kubernetes.client", clients)
    app.mysql_command = lambda member, sql: cluster.sql_calls.append((member, sql)) or cluster.events.append(
        "replication"
    )
    app._await_replication = lambda: cluster.events.append("gtid")
    original_exec = cluster.exec_command_checked

    def loaded_configuration(command, **kwargs):
        if "/etc/codehub/topology.json" in command:
            namespace = command.split()[2]
            return hashlib.sha256(
                cluster.maps[(namespace, "topology-config")].data["topology.json"].encode()
            ).hexdigest()
        return original_exec(command, **kwargs)

    cluster.exec_command_checked = loaded_configuration
    monkeypatch.setattr(app, "_serving_route_pods", lambda region, role: ("topology-current",))
    return app


def test_normal_database_link_install_uses_declared_data_only_and_converges_before_return(linked_app):
    app = linked_app
    endpoints = app.install_database_links(app.database_links)
    assert len(app.kubectl.slice_patches) == len(app.database_links) == 4
    assert app.kubectl.events[:4] == ["slice-read"] * 4
    assert all(kwargs == {"_preload_content": False, "_request_timeout": 5} for _, _, kwargs in app.kubectl.slice_reads)
    assert all(
        response.read_requests == [codehub_module.DATABASE_LINK_SLICE_MAX_BYTES + 1]
        and response.closed
        and response.released
        for response in app.kubectl.slice_responses
    )
    for namespace, name, body in app.kubectl.slice_patches:
        assert body["endpoints"] == [{"addresses": ["172.17.0.1"], "conditions": {"ready": True}}]
        assert body["ports"][0]["port"] in set(app.database_links.values())
        assert body["metadata"]["ownerReferences"][0]["uid"] == app.kubectl.services[(namespace, name)].metadata.uid
    assert app.kubectl.events[-1] == "gtid"
    assert len(app.kubectl.sql_calls) == 4
    assert all(
        "SOURCE_HOST='mysql-link-g" in sql and "SOURCE_PORT=3306" in sql and "STOP REPLICA;" in sql
        for _, sql in app.kubectl.sql_calls
    )
    assert all(member.region == "region-b" for member, _ in app.kubectl.sql_calls)
    assert (
        endpoints[("region-b", "region-a", "group-1")] == "mysql-link-g1-to-region-a.codehub-region-b.svc.cluster.local"
    )
    topology = json.loads(app.kubectl.maps[("codehub-region-b", "topology-config")].data["topology.json"])
    assert topology["writer_host"] == endpoints[("region-b", "region-a", "group-0")]
    assert topology["candidate_host"].startswith("mysql-g0-candidate.codehub-region-b.")
    assert topology["automatic_promotion"] is True and topology["failure_threshold"] == 3
    assert app.kubectl.maps[("codehub-region-b", "topology-config")].data["other.txt"] == "unchanged"
    assert len(app.kubectl.deployment_patches) == 2
    assert sum("rollout status deployment/topology" in command for command, _ in app.kubectl.commands) == 2


@pytest.mark.parametrize("endpoints", [None, []])
def test_empty_endpoint_wire_forms_use_the_same_owned_declaration_checks(linked_app, endpoints):
    app = linked_app
    for namespace, name in app.kubectl.slices:
        app.kubectl.slice_payloads[(namespace, name)] = app.kubectl.slice_payload(namespace, name) | {
            "endpoints": endpoints
        }
    app.install_database_links(app.database_links)
    assert len(app.kubectl.slice_patches) == 4
    assert all(response.closed and response.released for response in app.kubectl.slice_responses)


@pytest.mark.parametrize("changed", ["uid", "port"])
def test_null_endpoint_wire_form_retains_ownership_and_exact_port_guards(linked_app, changed):
    app = linked_app
    key = next(reversed(app.kubectl.slices))
    payload = app.kubectl.slice_payload(*key) | {"endpoints": None}
    if changed == "uid":
        payload["metadata"]["uid"] = "replacement"
        field = "metadata.uid"
    else:
        payload["ports"][0]["port"] += 1
        field = "ports[0].port"
    app.kubectl.slice_payloads[key] = payload
    with pytest.raises(RuntimeError) as raised:
        app.install_database_links(app.database_links)
    assert str(raised.value) == f"Predeclared SQL endpoint field {field} missing or mismatched"
    assert app.kubectl.slice_patches == app.kubectl.deployment_patches == app.kubectl.sql_calls == []
    assert not app._database_links_installed
    assert all(response.closed and response.released for response in app.kubectl.slice_responses)


@pytest.mark.parametrize(
    ("invalid", "diagnostic"),
    [
        ("version", "apiVersion"),
        ("version-missing", "apiVersion"),
        ("kind", "kind"),
        ("kind-missing", "kind"),
        ("metadata", "metadata"),
        ("name", "metadata.name"),
        ("namespace", "metadata.namespace"),
        ("uid", "metadata.uid"),
        ("revision-missing", "metadata.resourceVersion"),
        ("revision-empty", "metadata.resourceVersion"),
        ("revision-type", "metadata.resourceVersion"),
        ("revision-size", "metadata.resourceVersion"),
        ("labels", "metadata.labels"),
        ("service-label", "metadata.labels.kubernetes.io/service-name"),
        ("address-type", "addressType"),
        ("endpoint-present", "endpoints"),
        ("endpoint-object", "endpoints"),
        ("endpoint-false", "endpoints"),
        ("ports-missing", "ports"),
        ("ports-extra", "ports"),
        ("port-object", "ports"),
        ("port-name", "ports[0].name"),
        ("port-protocol", "ports[0].protocol"),
        ("port-number", "ports[0].port"),
        ("port-boolean", "ports[0].port"),
        ("invalid-json", None),
        ("json-array", None),
        ("oversize", None),
    ],
)
def test_raw_endpoint_declaration_rejects_changes_before_any_mutation(linked_app, invalid, diagnostic):
    app = linked_app
    key = next(reversed(app.kubectl.slices))
    payload = app.kubectl.slice_payload(*key)
    if invalid == "version":
        payload["apiVersion"] = "discovery.k8s.io/v1beta1"
    elif invalid == "version-missing":
        del payload["apiVersion"]
    elif invalid == "kind":
        payload["kind"] = "Endpoints"
    elif invalid == "kind-missing":
        del payload["kind"]
    elif invalid == "metadata":
        payload["metadata"] = []
    elif invalid in ("name", "namespace", "uid"):
        payload["metadata"][invalid] = "replaced\nresponse-data-must-not-be-logged"
    elif invalid == "revision-missing":
        del payload["metadata"]["resourceVersion"]
    elif invalid.startswith("revision-"):
        payload["metadata"]["resourceVersion"] = {"empty": "", "type": 1, "size": "x" * 1025}[
            invalid.removeprefix("revision-")
        ]
    elif invalid == "labels":
        payload["metadata"]["labels"] = None
    elif invalid == "service-label":
        payload["metadata"]["labels"]["kubernetes.io/service-name"] = "unrelated"
    elif invalid == "address-type":
        payload["addressType"] = "IPv6"
    elif invalid.startswith("endpoint-"):
        payload["endpoints"] = {
            "present": [{"addresses": ["172.17.0.1"]}],
            "object": {},
            "false": False,
        }[invalid.removeprefix("endpoint-")]
    elif invalid == "ports-missing":
        del payload["ports"]
    elif invalid == "ports-extra":
        payload["ports"].append({"name": "other", "protocol": "TCP", "port": 18474})
    elif invalid == "port-object":
        payload["ports"] = [None]
    elif invalid.startswith("port-"):
        field, value = {
            "name": ("name", "other"),
            "protocol": ("protocol", "UDP"),
            "number": ("port", 18474),
            "boolean": ("port", True),
        }[invalid.removeprefix("port-")]
        payload["ports"][0][field] = value
    elif invalid == "invalid-json":
        payload = b"{invalid"
    elif invalid == "json-array":
        payload = b"[]"
    elif invalid == "oversize":
        payload = b" " * (codehub_module.DATABASE_LINK_SLICE_MAX_BYTES + 1)
    app.kubectl.slice_payloads[key] = payload
    with pytest.raises(RuntimeError, match="SQL endpoint") as raised:
        app.install_database_links(app.database_links)
    if diagnostic is not None:
        assert str(raised.value) == f"Predeclared SQL endpoint field {diagnostic} missing or mismatched"
    assert "response-data-must-not-be-logged" not in str(raised.value)
    assert len(app.kubectl.slice_reads) == 4
    assert app.kubectl.slice_patches == app.kubectl.deployment_patches == app.kubectl.sql_calls == []
    assert not app._database_links_installed
    assert all(response.closed and response.released for response in app.kubectl.slice_responses)
    assert all(
        response.read_requests == [codehub_module.DATABASE_LINK_SLICE_MAX_BYTES + 1]
        for response in app.kubectl.slice_responses
    )


@pytest.mark.parametrize("failure", ["status", "read"])
def test_raw_endpoint_response_is_released_on_transport_failure(linked_app, failure):
    app = linked_app
    if failure == "status":
        app.kubectl.slice_status = 503
    else:
        app.kubectl.slice_read_error = TimeoutError("bounded API read timed out")
    with pytest.raises((RuntimeError, TimeoutError)):
        app.install_database_links(app.database_links)
    assert app.kubectl.slice_patches == []
    assert not app._database_links_installed
    assert len(app.kubectl.slice_responses) == 1
    assert app.kubectl.slice_responses[0].closed and app.kubectl.slice_responses[0].released


@pytest.mark.parametrize("invalid", ["extra-port", "slice-owner", "service-selector", "missing-port", "wrong-runner"])
def test_link_declaration_mismatch_fails_before_any_endpoint_mutation(linked_app, invalid):
    app = linked_app
    if invalid == "extra-port":
        app.kubectl.policies[app.regions[-1].namespace].spec.egress[0].ports.append(
            SimpleNamespace(port=18474, protocol="TCP")
        )
    elif invalid == "missing-port":
        app.kubectl.policies[app.regions[-1].namespace].spec.egress[0].ports.pop()
    elif invalid == "slice-owner":
        next(reversed(app.kubectl.slices.values())).metadata.uid = "replacement"
    elif invalid == "service-selector":
        next(reversed(app.kubectl.services.values())).spec.selector = {"unrelated": "pod"}
    with pytest.raises((RuntimeError, ValueError)):
        app.install_database_links(
            app.database_links, runner_host="127.0.0.1" if invalid == "wrong-runner" else "172.17.0.1"
        )
    assert app.kubectl.slice_patches == []
    assert not app._database_links_installed


def test_promoted_api_writer_uses_local_candidate_without_rewriting_frozen_worker_routes(linked_app):
    app = linked_app
    app.install_database_links(app.database_links)
    original = copy.deepcopy(app.kubectl.maps[("codehub-region-b", "worker-database-routes")].data)
    other_alias = app.kubectl.services[("codehub-region-b", "mysql-g1-writer-route")].spec.external_name
    candidate = next(m for m in app.database_groups[0].members if m.role == "candidate")
    app.set_writer_route("region-b", candidate)
    assert app.kubectl.services[("codehub-region-b", "mysql-g0-writer-route")].spec.external_name.startswith(
        "mysql-g0-candidate.codehub-region-b."
    )
    assert app.kubectl.services[("codehub-region-b", "mysql-g1-writer-route")].spec.external_name == other_alias
    assert app.kubectl.maps[("codehub-region-b", "worker-database-routes")].data == original
    assert app.normal_connection_endpoint("region-a", candidate)[0].startswith("mysql-link-g0-to-region-b.")


def test_replication_failure_prevents_successful_link_preparation(linked_app):
    app = linked_app

    def failed():
        raise RuntimeError("GTID history did not converge")

    app._await_replication = failed
    with pytest.raises(RuntimeError, match="GTID"):
        app.install_database_links(app.database_links)


def test_stale_topology_configuration_cannot_pass_the_loaded_connection_gate(linked_app, monkeypatch):
    app = linked_app
    monkeypatch.setattr(app.kubectl, "exec_command_checked", lambda *a, **kw: "stale-projected-config")
    clock = iter((0, 0, 0, 1000))
    monkeypatch.setattr(codehub_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(codehub_module.time, "sleep", lambda _: None)
    with pytest.raises(TimeoutError, match="did not load"):
        app._await_topology_configuration(app.regions[0], "new normal configuration")


@pytest.mark.parametrize("invalid", ["", "Uppercase", "standard/../../other", "two..labels", "a" * 254])
def test_storage_class_rejects_invalid_names_before_cluster_access(invalid):
    with pytest.raises(ValueError, match="Storage class"):
        CodeHub(storage_class=invalid)


@pytest.mark.parametrize("selected", [None, "standard", "fast.storage"])
def test_storage_class_override_is_explicit_and_preserves_chart_default(app, selected):
    app.storage_class = selected
    captured = []
    original = app.kubectl.exec_command_checked

    def checked(command, **kwargs):
        if command.startswith("helm"):
            parts = shlex.split(command)
            captured.append(codehub_module.yaml.safe_load(Path(parts[parts.index("-f") + 1]).read_text()))
        return original(command, **kwargs)

    app.kubectl.exec_command_checked = checked
    app.deploy()
    assert len(captured) == 2
    if selected is None:
        assert all("storageClass" not in values for values in captured)
    else:
        assert all(values["storageClass"] == selected for values in captured)


def test_replication_password_fits_mysql_channel_limit_and_is_not_logged(app):
    app.deploy()
    password = app._credentials["replication-password"]
    assert len(password) == 32
    assert all(character in "0123456789abcdef" for character in password)
    assert all(password not in command for command, _ in app.kubectl.commands)
    assert any("SOURCE_PASSWORD='" + password + "'" in (sql or "") for _, sql in app.kubectl.commands)
    app.cleanup()


def test_physical_bootstrap_preflights_all_members_and_proves_snapshots_before_channels_or_schema(app):
    app.deploy()
    commands = app.kubectl.commands
    preflights = [index for index, (command, _) in enumerate(commands) if "test -r" in command]
    plugins = [index for index, (_, sql) in enumerate(commands) if "INSTALL PLUGIN clone" in (sql or "")]
    copies = [index for index, (_, sql) in enumerate(commands) if "CLONE INSTANCE FROM" in (sql or "")]
    proofs = [index for index, (_, sql) in enumerate(commands) if "'state',STATE" in (sql or "")]
    channels = [index for index, (_, sql) in enumerate(commands) if "CHANGE REPLICATION SOURCE TO" in (sql or "")]
    migration = next(index for index, (command, _) in enumerate(commands) if "codehub.migrate" in command)
    assert len(preflights) == len(plugins) == 4
    assert len(copies) == len(proofs) == len(channels) == 3
    assert max(preflights) < min(plugins)
    assert max(proofs) < min(channels) < migration
    assert all(copy < proof for copy, proof in zip(copies, proofs, strict=True))
    statements = "\n".join(sql or "" for _, sql in commands)
    assert "SOURCE_AUTO_POSITION=1" in statements
    for index in channels:
        sql = commands[index][1]
        interval = int(re.search(r"SOURCE_CONNECT_RETRY=(\d+)", sql).group(1))
        attempts = int(re.search(r"SOURCE_RETRY_COUNT=(\d+)", sql).group(1))
        assert interval == 1 and interval * attempts == 600
    assert all(
        word not in statements.upper()
        for word in ("GTID_PURGED", "GTID_NEXT", "SQL_SLAVE_SKIP_COUNTER", "RESET BINARY")
    )
    assert sum("DROP USER 'snapshot_copy'" in (sql or "") for _, sql in commands) == 4
    clone_passwords = {
        re.search(r"IDENTIFIED BY '([^']+)'", sql).group(1)
        for _, sql in commands
        if "CLONE INSTANCE FROM" in (sql or "")
    }
    assert len(clone_passwords) == 1
    assert all(password not in command for password in clone_passwords for command, _ in commands)


def test_large_physical_bootstrap_clones_every_reader_from_its_own_group(tmp_path, monkeypatch):
    cluster = FakeCluster(regions=3, groups=4)
    application = CodeHub(
        tier=TIERS["large"], chart_path=tmp_path, kubectl=cluster, lease_path=tmp_path / "campaign.lock"
    )
    application._validate_boundary = lambda: {
        "workload_storage_root": str(tmp_path),
        "trusted_storage_root": str(tmp_path),
    }
    monkeypatch.setattr(HostCapacity, "observe", lambda *_: HostCapacity(64, 256, 1000))
    monkeypatch.setattr("sregym.conductor.scenarios.codehub_capacity.capture_kernel_baseline", lambda _: {})
    application.deploy()
    assert application.inventory.phase == LifecyclePhase.HEALTHY
    assert len(cluster.clone_sources) == 16
    for (_namespace, pod), source in cluster.clone_sources.items():
        group = pod.split("-", 2)[1]
        assert source == f"mysql-{group}-writer.codehub-region-a.svc.cluster.local"
    assert sum(namespace == "codehub-region-c" for namespace, _ in cluster.clone_sources) == 4
    assert sum("CHANGE REPLICATION SOURCE TO" in (sql or "") for _, sql in cluster.commands) == 16
    application.cleanup()


@pytest.mark.parametrize("phase", [LifecyclePhase.CREATED, LifecyclePhase.HEALTHY, LifecyclePhase.HANDOFF])
def test_physical_bootstrap_cannot_run_outside_fresh_provisioning(app, phase):
    app.inventory = replace(app.inventory, phase=phase)
    with pytest.raises(RuntimeError, match="fresh provisioning owner"):
        app._initialize_replication()
    assert app.kubectl.commands == []


@pytest.mark.parametrize(
    "override",
    [
        {"tables": 1},
        {"channels": 1},
        {"read_only": 1},
        {"version": "8.4.7"},
        {"gtid_mode": "OFF"},
        {"log_bin": 0},
        {"uuid": "38cc05cb-42ee-471a-9f64-6ba1578da0c7"},
    ],
)
def test_existing_data_or_changed_replication_configuration_is_rejected_before_snapshot_changes(app, override):
    app.kubectl.bootstrap_override = override
    with pytest.raises(RuntimeError, match="Fresh"):
        app.deploy()
    assert not any("INSTALL PLUGIN" in (sql or "") for _, sql in app.kubectl.commands)
    assert not app.kubectl.clone_sources
    assert app.inventory.phase == LifecyclePhase.STOPPED
    assert not app.kubectl.namespaces


@pytest.mark.parametrize("change", ["ownership", "image", "plugin"])
def test_snapshot_requires_owned_pinned_database_and_present_plugin_before_mutation(app, monkeypatch, change):
    if change == "ownership":
        app.kubectl.changed_database_uid = True
    elif change == "image":
        app.kubectl.mysql_image = "mysql:latest"
    else:
        original = app.kubectl.exec_command_checked

        def missing_library(command, **kwargs):
            if "test -r" in command:
                raise RuntimeError("Clone library is absent")
            return original(command, **kwargs)

        monkeypatch.setattr(app.kubectl, "exec_command_checked", missing_library)
    with pytest.raises(RuntimeError):
        app.deploy()
    assert not any("INSTALL PLUGIN" in (sql or "") for _, sql in app.kubectl.commands)
    assert not app.kubectl.clone_sources
    assert not app.kubectl.namespaces


def test_snapshot_never_adopts_a_previous_clone_result(app):
    app.kubectl.previous_clones = 1
    with pytest.raises(RuntimeError, match="earlier clone"):
        app.deploy()
    assert not app.kubectl.clone_sources
    assert not any("codehub.migrate" in command for command, _ in app.kubectl.commands)


@pytest.mark.parametrize(
    "invalid",
    [
        {"state": "Failed"},
        {"source": "unrelated-source:3306"},
        {"error": 1396},
        {"covered": 0},
        {"applied": 0},
        {"uuid": "38cc05cb-42ee-471a-9f64-6ba1578da0c7"},
    ],
)
def test_snapshot_completion_requires_real_donor_and_applied_gtid_proof(app, invalid, caplog):
    app.kubectl.clone_overrides = invalid
    with pytest.raises(RuntimeError, match="Physical replica"):
        app.deploy()
    assert len(app.kubectl.clone_sources) == 1
    assert not any("CHANGE REPLICATION SOURCE TO" in (sql or "") for _, sql in app.kubectl.commands)
    assert not any("codehub.migrate" in command for command, _ in app.kubectl.commands)
    assert "Duplicate CREATE USER" in caplog.text
    assert app.inventory.phase == LifecyclePhase.STOPPED


def test_clone_cli_restart_error_requires_valid_post_restart_proof(app):
    app.kubectl.clone_error = "ERROR 3707: mysqld must be restarted after cloning"
    app.deploy()
    assert app.inventory.phase == LifecyclePhase.HEALTHY
    assert len(app.kubectl.clone_sources) == 3


def test_clone_cli_restart_error_cannot_override_failed_snapshot(app):
    app.kubectl.clone_error = "ERROR 3707: mysqld must be restarted after cloning"
    app.kubectl.clone_overrides = {"state": "Failed"}
    with pytest.raises(RuntimeError, match="did not complete"):
        app.deploy()
    assert not any("CHANGE REPLICATION SOURCE TO" in (sql or "") for _, sql in app.kubectl.commands)


def test_clone_recipient_restart_connection_is_retried_until_real_snapshot_proof(app, monkeypatch):
    original = app.kubectl.exec_command_checked
    unavailable = set()

    def restarting(command, input_data=None, **kwargs):
        if "'state',STATE" in (input_data or "") and command not in unavailable:
            unavailable.add(command)
            raise OSError("MySQL is restarting")
        return original(command, input_data=input_data, **kwargs)

    monkeypatch.setattr(app.kubectl, "exec_command_checked", restarting)
    monkeypatch.setattr(codehub_module.time, "sleep", lambda _seconds: None)
    app.deploy()
    assert app.inventory.phase == LifecyclePhase.HEALTHY
    assert len(unavailable) == len(app.kubectl.clone_sources) == 3


def test_snapshot_wait_is_bounded_and_cannot_accept_incomplete_copy(app, monkeypatch):
    app.kubectl.clone_overrides = {"state": "In Progress"}
    times = iter((0, 0, 0, 0, 1, app.readiness_seconds + 1))
    monkeypatch.setattr(codehub_module.time, "monotonic", lambda: next(times, app.readiness_seconds + 1))
    monkeypatch.setattr(codehub_module.time, "sleep", lambda _seconds: None)
    with pytest.raises(TimeoutError, match="proven complete snapshot"):
        app.deploy()
    assert not any("codehub.migrate" in command for command, _ in app.kubectl.commands)
    assert not app.kubectl.namespaces


def test_replication_error_capture_preserves_sql_cause_and_redacts_credentials(app, monkeypatch, caplog):
    app.deploy()
    member = next(member for member in app.database_groups[0].members if member.role == "reader")
    password = app._credentials["replication-password"]
    temporary = "temporary-snapshot-credential"
    app.kubectl.diagnostics["last_sql_error"][0]["message"] = (
        f"Duplicate CREATE USER with {password} or {temporary}; IDENTIFIED BY 'other-secret'"
    )
    diagnostic = app._replication_diagnostics(member, extra_secrets=(temporary,))
    assert "1396" in diagnostic and "Duplicate CREATE USER" in diagnostic
    assert all(secret not in diagnostic + caplog.text for secret in (password, temporary, "other-secret"))
    original = app.mysql_command

    def disconnected(member, sql, **kwargs):
        if "WAIT_FOR_EXECUTED_GTID_SET" in sql:
            raise OSError("Database connection closed")
        return original(member, sql, **kwargs)

    monkeypatch.setattr(app, "mysql_command", disconnected)
    with pytest.raises(RuntimeError, match="observation failed.*Duplicate CREATE USER"):
        app._await_replication()


@pytest.mark.parametrize("failure", ["node_api", "missing_regions"])
def test_admitted_deploy_discovery_failure_releases_lease_before_namespaces(app, monkeypatch, failure):
    if failure == "node_api":

        def fail():
            raise OSError("Node inventory unavailable")

        monkeypatch.setattr(app.kubectl.core_v1_api, "list_node", fail)
    else:
        monkeypatch.setattr(app.kubectl.core_v1_api, "list_node", lambda: SimpleNamespace(items=[]))
    with pytest.raises((OSError, ValueError)):
        app.deploy()
    assert app._environment_lease is None
    assert not app.inventory.resources
    assert not app.kubectl.namespaces


def test_actual_snapshot_excludes_held_lease_without_releasing_owner(app, tmp_path):
    from sregym.service.verifier_state import restore_oracle, snapshot_oracle

    app.deploy()
    payload, resources = snapshot_oracle(SimpleNamespace(app=app), tmp_path)
    restored = restore_oracle(payload, lambda index: None)
    assert resources == [] and restored.app._environment_lease is None
    assert app._environment_lease.handle is not None
    competing = CodeHub(chart_path=app.chart_path, kubectl=FakeCluster(), lease_path=app.lease_path)
    competing._validate_boundary = app._validate_boundary
    with pytest.raises(RuntimeError, match="already owns"):
        competing.deploy()
    app.cleanup()


def test_capacity_rejects_nearly_full_separate_owner_filesystem_before_nodes(app, monkeypatch):
    owner_root = app.lease_path.parent / "private-owner-store"
    app.owner_storage_root = owner_root
    monkeypatch.setattr(HostCapacity, "observe", lambda path: HostCapacity(64, 256, 1 if path == owner_root else 1000))
    with pytest.raises(ValueError, match="control storage"):
        app.deploy()
    assert app._environment_lease is None and not app.kubectl.node_reads and not app.kubectl.namespaces
