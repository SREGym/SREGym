from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.problems.apps.frappe.controllers import ServiceWrongPodSelectionIA


def test_selector_injection_uses_numeric_port_and_preserves_original_route(offline_port_cluster):
    problem = ServiceWrongPodSelectionIA()
    service = SimpleNamespace(
        spec=SimpleNamespace(selector={"app": "gunicorn"}, ports=[SimpleNamespace(port=8000, target_port="http")])
    )
    problem.kubectl.core_v1_api.read_namespaced_service.return_value = service
    problem._patch_route_label = Mock()
    problem._set_route = Mock()
    problem.inject_fault()
    problem._set_route.assert_called_with(problem.faulty_service_selector, 8000)

    service.spec.selector = dict(problem.faulty_service_selector)
    service.spec.ports[0].target_port = 8000
    problem.inject_fault()
    problem.recover_fault()
    problem._set_route.assert_called_with({"app": "gunicorn"}, "http")


def test_partial_selector_injection_can_restore_the_original_route(offline_port_cluster):
    problem = ServiceWrongPodSelectionIA()
    problem.kubectl.core_v1_api.read_namespaced_service.return_value = SimpleNamespace(
        spec=SimpleNamespace(selector={"app": "gunicorn"}, ports=[SimpleNamespace(port=8000, target_port="http")])
    )
    problem._patch_route_label = Mock(side_effect=RuntimeError("rollout failed"))
    problem._set_route = Mock()
    with pytest.raises(RuntimeError, match="rollout failed"):
        problem.inject_fault()
    problem._patch_route_label.side_effect = None
    problem.recover_fault()
    problem._set_route.assert_called_once_with({"app": "gunicorn"}, "http")
