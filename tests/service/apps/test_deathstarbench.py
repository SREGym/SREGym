import copy
import json
from unittest.mock import Mock

import pytest

from sregym.service.apps.deathstarbench import (
    LABEL,
    SCALE_TIERS,
    ScaledHotelReservation,
    ScaledSocialNetwork,
    member_hosts,
    mongo_resources,
)


@pytest.mark.parametrize("tier", SCALE_TIERS)
def test_hotel_renders_real_replica_sets_without_mutating_source(tier):
    app = ScaledHotelReservation(tier)
    original = app.source_documents()
    source = copy.deepcopy(original)
    app.source_documents = lambda: source
    docs = app.render()
    sets = [d for d in docs if d["kind"] == "StatefulSet"]
    assert len(sets) == 6
    assert not any(d["kind"] in {"PersistentVolume", "PersistentVolumeClaim"} for d in docs)
    for stateful in sets:
        assert stateful["spec"]["replicas"] == app.scale.members
        assert stateful["spec"]["volumeClaimTemplates"][0]["spec"]["accessModes"] == ["ReadWriteOnce"]
        name = stateful["metadata"]["name"]
        service = next(d for d in docs if d["kind"] == "Service" and d["metadata"]["name"] == name)
        assert service["spec"]["selector"] == {LABEL: name}
        headless = next(d for d in docs if d["kind"] == "Service" and d["metadata"]["name"] == name + "-members")
        assert headless["spec"]["publishNotReadyAddresses"]
        assert stateful["spec"]["podManagementPolicy"] == "Parallel"
    config = json.loads(next(d for d in docs if d["kind"] == "ConfigMap")["data"]["config.json"])
    assert config["GeoMongoAddress"].endswith("/?replicaSet=mongodb-geo")
    assert config["GeoMongoAddress"].count(":27017") == app.scale.members
    # Source files are unchanged, despite rendering a different deployment topology.
    assert original == ScaledHotelReservation(tier).source_documents()


def test_invalid_tier_fails_before_cluster_access():
    with pytest.raises(ValueError, match="Unknown DeathStarBench tier"):
        ScaledHotelReservation("typo")


def test_social_client_seed_list_preserves_upstream_port_append_contract():
    app = ScaledSocialNetwork("replicated")
    app.databases = ["user-mongodb"]
    docs = [
        {
            "kind": "ConfigMap",
            "data": {
                "service-config.json": json.dumps(
                    {"user-mongodb": {"addr": "user-mongodb", "port": 27017}, "user-service": {"addr": "user-service"}}
                )
            },
        }
    ]
    app.patch_clients(docs)
    config = json.loads(docs[0]["data"]["service-config.json"])
    address = config["user-mongodb"]["addr"] + ":27017"
    assert address.split(",") == member_hosts("user-mongodb", "social-network", 3)
    assert config["user-service"]["addr"] == "user-service"


def test_authenticated_members_have_private_key_and_bounded_cache():
    docs = mongo_resources("mongodb-rate", "hotel-reservation", 3, True, "standard")
    pod = next(d for d in docs if d["kind"] == "StatefulSet")["spec"]["template"]["spec"]
    assert "--keyFile" in pod["containers"][0]["args"]
    assert "chmod 400" in pod["initContainers"][0]["command"][-1]
    assert pod["containers"][0]["resources"]["limits"]["memory"] == "768Mi"


def test_bootstrap_refuses_to_force_existing_replica_set_configuration():
    app = ScaledHotelReservation()
    app.mongo = Mock()
    app.wait_database = Mock()
    app._bootstrap_database("mongodb-user")
    script = app.mongo.call_args.args[1]
    assert "status.code === 94" in script
    assert "rs.initiate" in script
    assert "reconfig" not in script


def test_source_database_count_drift_is_rejected():
    app = ScaledHotelReservation()
    app.source_documents = lambda: []
    with pytest.raises(ValueError, match="Expected 6 MongoDB deployments"):
        app.render()


def test_social_redeployment_does_not_reseed_existing_incident_state():
    app = ScaledSocialNetwork()
    app.command = Mock(return_value="configmap/business-data-seeded\n")
    app.mongo_primary = Mock()
    app.apply = Mock()
    app.seed_data()
    assert app.command.call_count == 1
    app.mongo_primary.assert_not_called()
    app.apply.assert_not_called()


def test_social_media_readiness_matches_its_service_and_nginx_listener():
    app = ScaledSocialNetwork("replicated")
    documents = app.render()
    deployment = next(d for d in documents if d["kind"] == "Deployment" and d["metadata"]["name"] == "media-frontend")
    service = next(d for d in documents if d["kind"] == "Service" and d["metadata"]["name"] == "media-frontend")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["readinessProbe"]["tcpSocket"]["port"] == service["spec"]["ports"][0]["targetPort"] == 8080
    volume = next(v for v in deployment["spec"]["template"]["spec"]["volumes"] if v["name"] == "media-frontend-config")
    config = next(
        d for d in documents if d["kind"] == "ConfigMap" and d["metadata"]["name"] == volume["configMap"]["name"]
    )
    assert "listen       8080 reuseport;" in config["data"]["nginx.conf"]
