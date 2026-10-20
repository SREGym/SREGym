import base64
import configparser
import hashlib
import json

import pytest

from sregym.service.apps.openstack import application, config
from sregym.service.apps.openstack.application import PASSWORD_KEYS, OpenStackCloud
from sregym.service.apps.openstack.runtime.host_access_reconciler import broker_password as reconciler_password
from sregym.service.apps.openstack.runtime.hypervisor import broker_password as hypervisor_password
from sregym.service.apps.openstack.tiers import TIERS


@pytest.fixture(autouse=True)
def no_cluster(monkeypatch):
    # Rendering needs no cluster; KubeCtl() exits when no kubeconfig is present.
    monkeypatch.setattr(application, "KubeCtl", lambda: None)


def _app(tier="small"):
    app = OpenStackCloud(tier)
    app.passwords = {key: f"pw-{key}" for key in PASSWORD_KEYS}
    return app


def _by_kind(documents, kind):
    return {d["metadata"]["name"]: d for d in documents if d["kind"] == kind}


def test_every_rack_is_a_statefulset_of_named_hypervisors():
    for name, tier in TIERS.items():
        app = _app(name)
        sets = _by_kind(app.render_hypervisors(), "StatefulSet")
        assert sorted(sets) == sorted(f"hv-{rack}" for rack in tier.racks)
        assert {s["spec"]["replicas"] for s in sets.values()} == {tier.hosts_per_rack}
        assert len(tier.hypervisors) == len(tier.racks) * tier.hosts_per_rack
        assert set(tier.maintenance_hosts) <= set(tier.hypervisors)


def test_reconciler_reads_every_rack_as_an_optional_source():
    app = _app("regional")
    cron = _by_kind(app.render_host_access(), "CronJob")["host-access-reconciler"]
    volumes = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["volumes"]
    sources = next(v for v in volumes if v["name"] == "inventory")["projected"]["sources"]
    assert [s["configMap"]["name"] for s in sources] == [f"rack-{r}-inventory" for r in app.tier.racks]
    # The latent defect the host-access incident depends on.
    assert all(s["configMap"]["optional"] for s in sources)
    inventories = _by_kind(app.render_host_access(), "ConfigMap")
    hosts = [h["name"] for cm in inventories.values() for h in json.loads(cm["data"]["inventory.json"])["hosts"]]
    assert sorted(hosts) == sorted(app.tier.hypervisors)


def test_hypervisors_and_reconciler_derive_the_same_broker_credential():
    key = b"0123"
    assert hypervisor_password(key, "hv-r1-0") == reconciler_password(key, "hv-r1-0")
    assert hypervisor_password(key, "hv-r1-0") != hypervisor_password(key, "hv-r1-1")


def test_tenant_workload_is_hidden_from_agents_and_outside_the_cloud_namespace():
    app = _app()
    documents = app.render_tenants()
    assert all(d["metadata"]["labels"]["app"] == "load-generator" for d in documents)
    deployment = _by_kind(documents, "Deployment")["tenant-workload"]
    assert deployment["spec"]["template"]["metadata"]["labels"]["app"] == "load-generator"
    assert app.tenant_namespace != app.namespace


def test_service_configs_parse_and_point_at_namespaced_services():
    app = _app()
    endpoints = config.Endpoints("openstack")
    nova = configparser.ConfigParser(interpolation=None)
    nova.read_string(config.nova_conf(endpoints, app.passwords))
    assert nova["DEFAULT"]["compute_driver"] == "fake.MediumFakeDriver"
    assert nova["placement"]["auth_url"] == "http://keystone.openstack.svc:5000/v3"
    assert "rabbitmq.openstack.svc" in nova["DEFAULT"]["transport_url"]
    neutron = configparser.ConfigParser(interpolation=None)
    neutron.read_string(config.neutron_conf(endpoints, app.passwords))
    assert neutron["ml2"]["mechanism_drivers"] == ""


def test_rabbitmq_definitions_hash_passwords_the_way_rabbitmq_does():
    definitions = json.loads(config.rabbitmq_definitions({"rabbit_admin": "a", "rabbit_openstack": "b"}, b"salt"))
    users = {u["name"]: u for u in definitions["users"]}
    raw = base64.b64decode(users["openstack"]["password_hash"])
    assert raw[:4] == b"salt" and raw[4:] == hashlib.sha256(b"salt" + b"b").digest()
    # Hypervisor users belong to the reconciler, never to boot-time definitions.
    assert set(users) == {"admin", "openstack"}


def test_object_storage_puts_one_storage_node_per_rack_and_serves_rings_from_the_builder():
    from sregym.service.apps.openstack import object_storage

    app = OpenStackCloud("regional", object_storage=True)
    app.passwords = {key: f"pw-{key}" for key in PASSWORD_KEYS}
    documents = object_storage.render(app)
    storage = _by_kind(documents, "StatefulSet")["swift-storage"]
    assert storage["spec"]["replicas"] == len(app.tier.racks) == len(app.tier.storage_nodes)
    env = {e["name"]: e["value"] for e in storage["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["DEVICES"] == "d0 d1 d2"
    assert _by_kind(documents, "Service")["swift-storage"]["spec"]["publishNotReadyAddresses"] is True
    builder = _by_kind(documents, "Deployment")["swift-ring-builder"]
    assert builder["spec"]["strategy"] == {"type": "Recreate"}
    # Every Swift pod installs rings from the builder's ring service.
    for name in ("swift-proxy",):
        containers = _by_kind(documents, "Deployment")[name]["spec"]["template"]["spec"]["containers"]
        assert "ring-sync" in [c["name"] for c in containers]
    tenants = {d["metadata"]["name"]: d for d in app.render_tenants()}
    verifier = tenants["region-verifier"]["spec"]["template"]["spec"]["containers"]
    assert [c["name"] for c in verifier] == ["verifier", "object-verifier"]


def test_swift_server_configs_bind_the_ring_ports():
    for kind, port in config.SWIFT_PORTS.items():
        parsed = configparser.ConfigParser(interpolation=None)
        parsed.read_string(config.swift_server_conf(kind, 30))
        assert parsed["DEFAULT"]["bind_port"] == str(port)
        assert parsed["DEFAULT"]["mount_check"] == "false"
