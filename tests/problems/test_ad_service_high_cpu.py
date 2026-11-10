import json
import shlex
from unittest.mock import Mock

import pytest
from kubernetes.client.rest import ApiException

from sregym.conductor.problems.ad_service_high_cpu import AdServiceHighCpu


def ad_pod(cpu="100m", *, name="ad-current", deleting=False):
    return {
        "metadata": {"name": name, "deletionTimestamp": "2026-11-06T00:00:00Z" if deleting else None},
        "spec": {
            "containers": [
                {"name": "sidecar", "resources": {"limits": {"cpu": "10m"}}},
                {"name": "ad", "resources": {"limits": {"cpu": cpu, "memory": "512Mi"}}},
            ]
        },
        "status": {"phase": "Running", "containerStatuses": [{"name": "ad", "state": {"running": {}}}]},
    }


@pytest.fixture
def problem():
    instance = AdServiceHighCpu.__new__(AdServiceHighCpu)
    instance.namespace = "astronomy-shop"
    instance.faulty_service = "ad"
    instance.feature_flag = "adHighCpu"
    instance.cpu_limit = "100m"
    instance.fault_injected = False
    instance.injector = Mock()
    instance.kubectl = Mock()
    instance.kubectl.exec_command_checked.side_effect = [
        "patched",
        "100m\n",
        "rollout complete",
        json.dumps({"items": [ad_pod()]}),
    ]
    return instance


def patch_body(command: str) -> str:
    args = shlex.split(command)
    return args[args.index("-p") + 1]


def test_inject_applies_a_valid_patch_and_waits_for_the_limited_container(problem):
    problem.inject_fault()

    problem.injector.inject_fault.assert_called_once_with("adHighCpu")
    calls = problem.kubectl.exec_command_checked.call_args_list
    assert len(calls) == 4
    patch_cmd, readback_cmd, rollout_cmd, pods_cmd = (call.args[0] for call in calls)
    assert patch_cmd.startswith("kubectl patch deployment ad -n astronomy-shop --type=strategic")
    body = json.loads(patch_body(patch_cmd))
    assert body["spec"]["template"]["spec"]["containers"] == [{"name": "ad", "resources": {"limits": {"cpu": "100m"}}}]
    assert readback_cmd.startswith("kubectl get deployment ad -n astronomy-shop")
    assert "resources.limits.cpu" in readback_cmd
    assert rollout_cmd == "kubectl rollout status deployment/ad -n astronomy-shop --timeout=120s"
    assert pods_cmd == "kubectl get pods -n astronomy-shop -l opentelemetry.io/name=ad -o json"
    assert [call.kwargs["timeout"] for call in calls] == [30, 30, 130, 30]
    assert problem.fault_injected is True
    problem.kubectl.exec_command.assert_not_called()


def test_inject_raises_when_the_template_limit_did_not_take(problem):
    problem.kubectl.exec_command_checked.side_effect = ["patched", "\n"]

    with pytest.raises(RuntimeError, match="did not take"):
        problem.inject_fault()

    assert problem.fault_injected is False
    assert problem.kubectl.exec_command_checked.call_count == 2


@pytest.mark.parametrize("failed_call", [0, 1, 2, 3])
def test_inject_preserves_command_failures_and_does_not_mark_success(problem, failed_call):
    results = ["patched", "100m", "rollout complete", json.dumps({"items": [ad_pod()]})]
    results[failed_call] = RuntimeError("API or rollout failed")
    problem.kubectl.exec_command_checked.side_effect = results

    with pytest.raises(RuntimeError, match="API or rollout failed"):
        problem.inject_fault()

    assert problem.fault_injected is False
    assert problem.kubectl.exec_command_checked.call_count == failed_call + 1


@pytest.mark.parametrize("cpu", [None, "500m"])
def test_template_success_does_not_hide_a_wrong_runtime_limit(problem, cpu):
    problem.kubectl.exec_command_checked.side_effect = [
        "patched",
        "100m",
        "rollout complete",
        json.dumps({"items": [ad_pod(cpu)]}),
    ]

    with pytest.raises(RuntimeError, match="not running with CPU limit"):
        problem.inject_fault()

    assert problem.fault_injected is False


@pytest.mark.parametrize("state", ["pending", "waiting", "no_status", "no_container"])
def test_inject_rejects_a_container_that_has_not_started(problem, state):
    pod = ad_pod()
    if state == "pending":
        pod["status"]["phase"] = "Pending"
    elif state == "waiting":
        pod["status"]["containerStatuses"][0]["state"] = {"waiting": {"reason": "ContainerCreating"}}
    elif state == "no_status":
        pod["status"].pop("containerStatuses")
    else:
        pod["spec"]["containers"] = [pod["spec"]["containers"][0]]
    problem.kubectl.exec_command_checked.side_effect = [
        "patched",
        "100m",
        "rollout complete",
        json.dumps({"items": [pod]}),
    ]

    with pytest.raises(RuntimeError, match="not running with CPU limit"):
        problem.inject_fault()

    assert problem.fault_injected is False


@pytest.mark.parametrize("pods", [[], [ad_pod(deleting=True)]])
def test_inject_requires_a_current_pod(problem, pods):
    problem.kubectl.exec_command_checked.side_effect = [
        "patched",
        "100m",
        "rollout complete",
        json.dumps({"items": pods}),
    ]

    with pytest.raises(RuntimeError, match="No current ad pods"):
        problem.inject_fault()

    assert problem.fault_injected is False


def test_inject_ignores_an_old_terminating_pod(problem):
    pods = [ad_pod(None, name="ad-old", deleting=True), ad_pod()]
    problem.kubectl.exec_command_checked.side_effect = [
        "patched",
        "100m",
        "rollout complete",
        json.dumps({"items": pods}),
    ]

    problem.inject_fault()

    assert problem.fault_injected is True


def test_recover_removes_only_the_named_containers_cpu_limit(problem):
    problem.fault_injected = True
    problem.recover_fault()

    problem.injector.recover_fault.assert_called_once_with("adHighCpu")
    problem.kubectl.apps_v1_api.patch_namespaced_deployment.assert_called_once_with(
        name="ad",
        namespace="astronomy-shop",
        body={"spec": {"template": {"spec": {"containers": [{"name": "ad", "resources": {"limits": {"cpu": None}}}]}}}},
        _request_timeout=30,
    )
    assert problem.fault_injected is False
    problem.kubectl.exec_command_checked.assert_not_called()


@pytest.mark.parametrize("status", [403, 409, 500])
def test_recover_propagates_api_errors_and_preserves_fault_state(problem, status):
    problem.fault_injected = True
    problem.kubectl.apps_v1_api.patch_namespaced_deployment.side_effect = ApiException(status=status, reason="rejected")

    with pytest.raises(ApiException) as caught:
        problem.recover_fault()

    assert caught.value.status == status
    assert problem.fault_injected is True


def test_recover_accepts_an_already_removed_original_deployment(problem):
    problem.fault_injected = True
    problem.kubectl.apps_v1_api.patch_namespaced_deployment.side_effect = ApiException(status=404)

    problem.recover_fault()

    assert problem.fault_injected is False


def test_recover_preserves_feature_flag_recovery_failures(problem):
    problem.fault_injected = True
    problem.injector.recover_fault.side_effect = RuntimeError("flag recovery failed")

    with pytest.raises(RuntimeError, match="flag recovery failed"):
        problem.recover_fault()

    assert problem.fault_injected is True
    problem.kubectl.apps_v1_api.patch_namespaced_deployment.assert_not_called()
