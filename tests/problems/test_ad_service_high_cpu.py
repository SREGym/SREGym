import json
import shlex
from unittest.mock import Mock

import pytest

from sregym.conductor.problems.ad_service_high_cpu import AdServiceHighCpu


@pytest.fixture
def problem():
    instance = AdServiceHighCpu.__new__(AdServiceHighCpu)
    instance.namespace = "astronomy-shop"
    instance.faulty_service = "ad"
    instance.feature_flag = "adHighCpu"
    instance.cpu_limit = "100m"
    instance.injector = Mock()
    instance.kubectl = Mock()
    instance.kubectl.exec_command_checked.return_value = "100m"
    return instance


def patch_body(command: str) -> str:
    args = shlex.split(command)
    return args[args.index("-p") + 1]


def test_inject_applies_a_valid_strategic_patch_and_checks_it(problem):
    problem.inject_fault()

    problem.injector.inject_fault.assert_called_once_with("adHighCpu")
    calls = problem.kubectl.exec_command_checked.call_args_list
    assert len(calls) == 2
    patch_cmd, readback_cmd = (call.args[0] for call in calls)
    assert patch_cmd.startswith("kubectl patch deployment ad -n astronomy-shop --type=strategic")
    body = json.loads(patch_body(patch_cmd))
    assert body["spec"]["template"]["spec"]["containers"] == [{"name": "ad", "resources": {"limits": {"cpu": "100m"}}}]
    assert readback_cmd.startswith("kubectl get deployment ad -n astronomy-shop")
    assert "resources.limits.cpu" in readback_cmd
    problem.kubectl.exec_command.assert_not_called()


def test_inject_raises_when_the_limit_did_not_take(problem):
    problem.kubectl.exec_command_checked.return_value = "\n"

    with pytest.raises(RuntimeError, match="did not take"):
        problem.inject_fault()


def test_recover_removes_the_limit_with_a_checked_command(problem):
    problem.recover_fault()

    problem.injector.recover_fault.assert_called_once_with("adHighCpu")
    (call,) = problem.kubectl.exec_command_checked.call_args_list
    assert call.args[0].startswith("kubectl patch deployment ad -n astronomy-shop --type=json")
    assert json.loads(patch_body(call.args[0])) == [
        {"op": "remove", "path": "/spec/template/spec/containers/0/resources/limits/cpu"}
    ]
    problem.kubectl.exec_command.assert_not_called()
