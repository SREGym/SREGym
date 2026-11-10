from pathlib import Path

import yaml

from sregym.service.apps.hotel_reservation import HotelReservation


def test_tracing_uses_the_central_collector_without_changing_source():
    app = HotelReservation()
    source = {
        path.relative_to(app.k8s_deploy_path): path.read_text() for path in Path(app.k8s_deploy_path).rglob("*.yaml")
    }
    traced = set()
    with app._rendered_deployment_configs() as rendered_path:
        for relative, original in source.items():
            expected = yaml.safe_load(original)
            actual = yaml.safe_load((rendered_path / relative).read_text())
            name = expected.get("metadata", {}).get("name")
            if expected.get("kind") == "Deployment" and name in app.services:
                container = expected["spec"]["template"]["spec"]["containers"][0]
                container["args"] = ["-jaegeraddr", "otel-collector.observe.svc.cluster.local:6831"]
                traced.add(name)
            assert actual == expected

    assert traced == app.services
    assert not rendered_path.exists()
    assert all((Path(app.k8s_deploy_path) / path).read_text() == content for path, content in source.items())


def test_standard_rate_policy_has_headroom_above_the_default_workload():
    app = HotelReservation()
    deployment_path = Path(app.k8s_deploy_path) / "rate" / "rate-deployment.yaml"
    deployment = yaml.safe_load(deployment_path.read_text())
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item["value"] for item in container["env"]}

    assert env["RATE_BACKEND_QPS_LIMIT"] == "500"


def test_deployment_env_overrides_are_rendered_without_changing_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    deployment_path = source / "rate.yaml"
    original = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: rate
spec:
  template:
    spec:
      containers:
        - name: hotel-reserv-rate
          command: [rate]
          args: ["-consuladdr", "consul:8500"]
          env:
            - name: JAEGER_SAMPLE_RATIO
              value: \"1\"
            - name: RATE_BACKEND_QPS_LIMIT
              value: \"500\"
"""
    deployment_path.write_text(original)

    app = HotelReservation.__new__(HotelReservation)
    app.k8s_deploy_path = source
    app.deployment_env_overrides = {
        "rate": {
            "hotel-reserv-rate": {
                "RATE_BACKEND_QPS_LIMIT": "20",
                "RATE_QUEUE_CAPACITY": "256",
            }
        }
    }

    with app._rendered_deployment_configs() as rendered_path:
        rendered = yaml.safe_load((Path(rendered_path) / "rate.yaml").read_text())
        env = rendered["spec"]["template"]["spec"]["containers"][0]["env"]
        assert {item["name"]: item["value"] for item in env} == {
            "JAEGER_SAMPLE_RATIO": "1",
            "RATE_BACKEND_QPS_LIMIT": "20",
            "RATE_QUEUE_CAPACITY": "256",
        }
        assert rendered["spec"]["template"]["spec"]["containers"][0]["args"] == [
            "-consuladdr",
            "consul:8500",
            "-jaegeraddr",
            "otel-collector.observe.svc.cluster.local:6831",
        ]

    assert deployment_path.read_text() == original


def test_deployment_env_overrides_reject_unknown_targets(tmp_path):
    app = HotelReservation.__new__(HotelReservation)
    app.k8s_deploy_path = tmp_path
    app.deployment_env_overrides = {"missing": {"container": {"SETTING": "value"}}}

    try:
        with app._rendered_deployment_configs():
            pass
    except RuntimeError as exc:
        assert "deployment/missing:container" in str(exc)
    else:
        raise AssertionError("missing override target was accepted")
