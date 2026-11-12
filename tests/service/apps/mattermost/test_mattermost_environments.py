from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.usefixtures("offline_kubectl")


def test_cascade_gateway_runs_several_replicas_and_is_not_oracle_topology_checked():
    """The shared SaaS oracle demands exactly one replica of its deployments.

    The gateway is meant to run several, and deleting the scaler is a legitimate
    repair, so neither may be an `auxiliary_deployment`.
    """
    from sregym.service.apps.mattermost.cascade import MattermostCascadeApplication

    app = MattermostCascadeApplication()
    docs = app.render()
    assert set(app.cascade_deployments).isdisjoint(app.auxiliary_deployments)
    gateway = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "chat-gateway")
    assert gateway["spec"]["replicas"] == app.capacity_floor >= 2
    # A recreate strategy would drop the whole gateway on every scale change.
    assert gateway["spec"]["strategy"]["type"] == "RollingUpdate"


def test_only_the_scaler_gets_an_api_token_and_only_over_the_gateway_scale():
    from sregym.service.apps.mattermost.cascade import MattermostCascadeApplication

    docs = MattermostCascadeApplication().render()
    pods = [d for d in docs if d["kind"] == "Deployment"]
    tokened = [d["metadata"]["name"] for d in pods if d["spec"]["template"]["spec"].get("automountServiceAccountToken")]
    assert tokened == ["capacity-scaler"]
    role = next(d for d in docs if d["kind"] == "Role")
    scale_rule = next(r for r in role["rules"] if r["resources"] == ["deployments/scale"])
    assert scale_rule["resourceNames"] == ["chat-gateway"]
    assert sorted(scale_rule["verbs"]) == ["get", "patch", "update"]
    # No write access to anything else, and no access to secrets at all.
    assert not any("secrets" in r["resources"] for r in role["rules"])


def test_cascade_control_state_is_persistent_so_a_restart_cannot_clear_it():
    from sregym.service.apps.mattermost.cascade import CONTROL_VOLUME, MattermostCascadeApplication

    app = MattermostCascadeApplication()
    assert CONTROL_VOLUME in app.data_volumes
    docs = app.render()
    assert any(d["kind"] == "PersistentVolumeClaim" and d["metadata"]["name"] == CONTROL_VOLUME for d in docs)
    for name in ("chat-gateway", "capacity-scaler"):
        deployment = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == name)
        volumes = deployment["spec"]["template"]["spec"]["volumes"]
        assert any(v.get("persistentVolumeClaim", {}).get("claimName") == CONTROL_VOLUME for v in volumes), name


def test_customer_traffic_is_concurrent_and_part_of_the_environment():
    """A sequential probe can never saturate a worker pool, however slow it gets."""
    from sregym.service.apps.mattermost.cascade import MattermostCascadeApplication

    app = MattermostCascadeApplication()
    docs = app.render()
    traffic = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "chat-traffic")
    source = traffic["spec"]["template"]["spec"]["containers"][0]["command"][-1]
    assert "threading.Thread" in source
    assert "chat-gateway:8080" in source
    # More concurrent clients than the gateway has workers across the floor.
    assert f"range({app.gateway_workers * app.capacity_floor})" in source
    # The conductor's inherited sequential workload would only muddy the signal.
    assert app.start_workload() is None


def test_the_cascade_family_ships_no_guide():
    """This test previously required the leak too.

    It asserted the guide stated that capacity taken away again is not recovery,
    and pointed at the metrics endpoint and the latency control file. The agent's
    first action on the old screen was to read that file and then execute it.
    """
    import sregym.service.apps.mattermost.cascade as cascade

    assert not hasattr(cascade, "CASCADE_GUIDE")
    description = cascade.MattermostCascadeApplication().get_app_json()["Desc"]
    assert "chat-gateway" in description
    assert "README" not in description


def test_calibrated_policy_still_reads_a_saturated_gateway_as_idle():
    """Thresholds come from measured healthy CPU, so they travel between hosts."""

    from sregym.service.apps.mattermost.cascade import MattermostCascadeApplication
    from sregym.service.apps.mattermost.runtime.capacity_scaler import DEFAULT_POLICY, decide

    app = MattermostCascadeApplication()
    app.healthy_cpu = Mock(return_value=44.0)
    written = {}
    app.write_control = Mock(side_effect=lambda name, content: written.__setitem__(name, content))

    policy = app.calibrate_scaler()

    assert policy["scale_in_below"] == 22
    assert policy["calibrated_healthy_cpu_percent"] == 44.0
    # Calibration is also what turns the automation on; before it, the scaler has
    # no policy file and deliberately does nothing.
    assert policy["enabled"] is True
    # Healthy load sits inside the band; a blocked pool falls below it.
    rules = {**DEFAULT_POLICY, **policy}
    assert decide(3, 44.0, rules, "cpu")[0] == 3
    assert decide(3, 2.0, rules, "cpu")[0] == 2
    assert "healthy gateway CPU: 44.0%" in written["capacity-policy.txt"]
