from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.oracles.target_port_mitigation import TargetPortMisconfigMitigationOracle


@pytest.mark.parametrize(
    "ports,value,expected",
    [
        ((9090,), 9090, True),
        ((9090,), 8000, False),
        (("http", 8000), "http", True),
        (("http", 8000), 8000, True),
        (("http", 8000), 9999, False),
    ],
)
def test_app_ports_use_real_unmodified_service_data(ports, value, expected):
    service = {"spec": {"ports": [{"targetPort": value}]}}
    original = deepcopy(service)
    kube = Mock()
    getter = kube.get_service_json
    getter.return_value = service
    kube.list_pods.return_value = SimpleNamespace(items=[])
    oracle = TargetPortMisconfigMitigationOracle(
        SimpleNamespace(kubectl=kube, namespace="app", faulty_service="frontend"), valid_target_ports=ports
    )
    assert oracle.evaluate()["success"] is expected
    assert service == original
    assert kube.get_service_json is getter
    getter.assert_called_once_with("frontend", "app")
