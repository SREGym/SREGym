from copy import deepcopy

import pytest

from sregym.service.kubernetes_access_policy import apply_json_patch, workload_adds_network_access


def workload(resource, spec):
    if resource == "pods":
        return {"spec": spec}
    template = {"spec": {"template": {"spec": spec}}}
    return {"spec": {"jobTemplate": template}} if resource == "cronjobs" else template


@pytest.mark.parametrize("resource", ["pods", "deployments", "daemonsets", "jobs", "cronjobs"])
@pytest.mark.parametrize(
    "setting",
    [
        {"hostNetwork": True},
        {"hostPID": True},
        {"containers": [{"name": "network", "securityContext": {"privileged": True}}]},
        {"initContainers": [{"name": "network", "securityContext": {"privileged": True}}]},
        {"volumes": [{"name": "host", "hostPath": {"path": "/lib/modules", "type": "Directory"}}]},
    ],
)
def test_existing_settings_and_removal_are_allowed_but_new_settings_are_not(resource, setting):
    current = workload(resource, setting)
    assert not workload_adds_network_access(resource, deepcopy(current), current)
    assert not workload_adds_network_access(resource, workload(resource, {}), current)
    assert workload_adds_network_access(resource, current, workload(resource, {}))


def test_similar_keys_in_user_data_are_not_pod_permissions():
    obj = workload("pods", {"containers": [{"name": "api", "env": [{"name": "privileged", "value": "true"}]}]})
    obj["metadata"] = {"annotations": {"hostNetwork": True, "hostPath": "/"}}
    assert not workload_adds_network_access("pods", obj)


def test_host_path_cannot_be_changed_or_transferred_to_a_new_volume():
    current = workload("pods", {"volumes": [{"name": "modules", "hostPath": {"path": "/lib/modules"}}]})
    changed = workload("pods", {"volumes": [{"name": "modules", "hostPath": {"path": "/"}}]})
    transferred = workload("pods", {"volumes": [{"name": "root", "hostPath": {"path": "/lib/modules"}}]})
    assert workload_adds_network_access("pods", changed, current)
    assert workload_adds_network_access("pods", transferred, current)


@pytest.mark.parametrize("operation", ["copy", "move"])
def test_patch_resolves_values_and_does_not_change_its_input(operation):
    current = workload(
        "pods",
        {
            "hostNetwork": True,
            "containers": [{"name": "api", "env": [{"name": "A", "value": "one"}, {"name": "B", "value": "two"}]}],
        },
    )
    original = deepcopy(current)
    result = apply_json_patch(
        current,
        [
            {"op": "test", "path": "/spec/hostNetwork", "value": True},
            {"op": operation, "from": "/spec/containers/0/env/0/value", "path": "/spec/containers/0/env/1/value"},
        ],
    )
    assert result["spec"]["containers"][0]["env"][1]["value"] == "one"
    assert current == original
    assert not workload_adds_network_access("pods", result, current)
