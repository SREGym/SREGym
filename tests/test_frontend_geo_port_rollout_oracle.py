import json
from types import SimpleNamespace

from sregym.conductor.oracles.frontend_geo_port_rollout_oracle import FrontendGeoPortRolloutMitigationOracle


def _frontend_pod(uid: str, name: str = "frontend-abc123"):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, uid=uid),
        status=SimpleNamespace(
            phase="Running",
            container_statuses=[SimpleNamespace(ready=True)],
        ),
    )


def _oracle(config_geo_port: str, runtime_geo_port: str, faulty_uid: str, current_uid: str):
    frontend_pod = _frontend_pod(uid=current_uid)
    configmap = SimpleNamespace(data={"service-config.json": json.dumps({"GeoPort": config_geo_port})})

    class DummyCoreV1Api:
        def read_namespaced_config_map(self, name, namespace):
            return configmap

        def read_namespaced_pod(self, name, namespace):
            return frontend_pod

    class DummyKubectl:
        def __init__(self):
            self.core_v1_api = DummyCoreV1Api()

        def get_pod_name(self, namespace, label_selector):
            return frontend_pod.metadata.name

        def exec_command(self, command: str):
            return json.dumps({"GeoPort": runtime_geo_port})

        def list_pods(self, namespace):
            return SimpleNamespace(items=[frontend_pod])

    problem = SimpleNamespace(
        namespace="hotel-reservation",
        pod_label_selector="io.kompose.service=frontend",
        config_path="/go/src/github.com/harlow/go-micro-services/config.json",
        configmap_name="frontend-config",
        expected_geo_port=8083,
        faulty_frontend_pod_uid=faulty_uid,
        kubectl=DummyKubectl(),
    )
    return FrontendGeoPortRolloutMitigationOracle(problem=problem)


def test_frontend_geo_port_rollout_oracle_requires_fixed_config_and_restart():
    oracle = _oracle(config_geo_port="8083", runtime_geo_port="8083", faulty_uid="old-uid", current_uid="new-uid")

    result = oracle.evaluate()

    assert result["success"] is True
    assert result["frontend_restarted"] is True
    assert result["config_geo_port"] == "8083"
    assert result["runtime_geo_port"] == "8083"


def test_frontend_geo_port_rollout_oracle_rejects_config_fix_without_restart():
    oracle = _oracle(config_geo_port="8083", runtime_geo_port="8083", faulty_uid="same-uid", current_uid="same-uid")

    result = oracle.evaluate()

    assert result["success"] is False
    assert result["frontend_restarted"] is False


def test_frontend_geo_port_rollout_oracle_rejects_unfixed_runtime_config():
    oracle = _oracle(config_geo_port="8083", runtime_geo_port="18083", faulty_uid="old-uid", current_uid="new-uid")

    result = oracle.evaluate()

    assert result["success"] is False
    assert result["config_geo_port"] == "8083"
    assert result["runtime_geo_port"] == "18083"
