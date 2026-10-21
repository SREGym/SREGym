"""WrongBinMitigationOracle must judge the executable the container runs, not
whether the string "profile" appears somewhere in its command."""

from types import SimpleNamespace

import pytest

from sregym.conductor.oracles.wrong_bin_mitigation import WrongBinMitigationOracle, _executable


@pytest.fixture(autouse=True)
def _no_rollout_wait(monkeypatch):
    monkeypatch.setattr(WrongBinMitigationOracle, "rollout_timeout_s", 0)


def _deployment(command, *, ready=1):
    return SimpleNamespace(
        metadata=SimpleNamespace(name="profile", generation=1, deletion_timestamp=None),
        spec=SimpleNamespace(
            replicas=1,
            template=SimpleNamespace(
                spec=SimpleNamespace(containers=[SimpleNamespace(name="hotel-reserv-profile", command=command)])
            ),
        ),
        status=SimpleNamespace(
            observed_generation=1,
            replicas=1,
            updated_replicas=1,
            ready_replicas=ready,
            available_replicas=ready,
            unavailable_replicas=1 - ready,
        ),
    )


def _evaluate(deployment, **problem_fields):
    kubectl = SimpleNamespace(get_deployment=lambda name, namespace: deployment)
    problem = SimpleNamespace(
        kubectl=kubectl, namespace="hotel-reservation", faulty_service="profile", **problem_fields
    )
    return WrongBinMitigationOracle(problem).evaluate()


def test_injected_geo_binary_fails():
    assert _evaluate(_deployment(["geo"]))["reason"] == "fault_still_present"


def test_profile_binary_passes():
    assert _evaluate(_deployment(["profile"])) == {"success": True}


def test_profile_binary_by_absolute_path_passes():
    """/go/bin/profile is the binary the comment in the oracle names."""
    assert _evaluate(_deployment(["/go/bin/profile"])) == {"success": True}


@pytest.mark.parametrize("command", [["geo", "profile"], ["sleep", "profile"]])
def test_profile_as_an_argument_fails(command):
    """Only the first element runs; "profile" as an argument does not."""
    assert _evaluate(_deployment(command))["reason"] == "fault_still_present"


def test_right_binary_that_never_becomes_ready_fails():
    verdict = _evaluate(_deployment(["profile"], ready=0))
    assert verdict["success"] is False


@pytest.mark.parametrize(
    "command",
    [
        ["env", "profile"],
        ["/usr/bin/env", "JAEGER_SAMPLE_RATIO=1", "/go/bin/profile"],
        ["env", "-i", "-u", "UNUSED", "--", "profile"],
        ["env", "--", "JAEGER_SAMPLE_RATIO=1", "profile"],
        ["sh", "-c", "profile"],
        ["sh", "-c", "exec /go/bin/profile"],
        ["bash", "-lc", "exec env JAEGER_SAMPLE_RATIO=1 profile"],
    ],
)
def test_persistent_launchers_pass(command):
    assert _evaluate(_deployment(command)) == {"success": True}


@pytest.mark.parametrize(
    "command",
    [
        ["env", "geo", "profile"],
        ["env", "--split-string=geo", "profile"],
        ["env", "-S", "geo", "profile"],
        ["sh", "-c", "geo profile"],
        ["sh", "-c", "echo profile; exec geo"],
        ["sh", "-c", "profile; exec geo"],
        ["sh", "-c", "profile && exec geo"],
        ["sh", "-c", "profile | geo"],
        ["sh", "-c", "profile $(geo)"],
        ["sh", "-c", "profile\ngeo"],
        ["sh", "-c", "profile '"],
    ],
)
def test_ambiguous_or_wrong_launchers_fail(command):
    assert _evaluate(_deployment(command))["reason"] == "fault_still_present"


@pytest.mark.parametrize("command", [[], ["env"], ["env", "-u"], ["sh", "-c", ""], ["sh", "-c", "exec"]])
def test_empty_or_incomplete_launchers_have_no_expected_executable(command):
    assert _executable(command) != "profile"


def test_launcher_without_a_completed_rollout_fails():
    assert _evaluate(_deployment(["env", "profile"], ready=0))["success"] is False


@pytest.mark.parametrize(
    "command,passes",
    [
        (["uvicorn", "saleor.asgi:application", "--port=8000"], True),
        ([], True),  # override removed, the image's default CMD runs
        (["celery", "-A", "saleor", "worker"], False),
    ],
)
def test_ports_with_expected_and_wrong_command(command, passes):
    """The Saleor port names its own binary and the injected one."""
    verdict = _evaluate(_deployment(command), expected_command="uvicorn", wrong_command="celery")
    assert verdict["success"] is passes
