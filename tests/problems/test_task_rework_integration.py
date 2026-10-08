"""Opt-in full application qualification for the portable TLS clock task."""

import json
import os
import shlex
import time
from pathlib import Path

import pytest

from sregym.conductor.problems.registry import ProblemRegistry
from sregym.conductor.problems.tls_clock_drift import (
    CLOCK_CONFIG,
    CLOCK_KEY,
    TLSClockDriftHotelReservation,
    tls_client_container,
    tls_client_volumes,
)
from sregym.service.container_runner import ContainerConfig, ContainerRunner, ExecInput
from sregym.service.docker_runtime import validate_rootless_boundary
from sregym.service.internet_policy import InternetPolicy
from sregym.service.verifier_runtime import VerifierRuntime
from sregym.service.verifier_state import snapshot_oracle

pytestmark = pytest.mark.integration


def test_tls_clock_healthy_noop_operator_repair_and_bypasses(tmp_path):
    filename = os.environ.get("SREGYM_ROOTLESS_TEST_KUBECONFIG")
    if not filename or os.environ.get("SREGYM_ROOTLESS_WORKLOAD") != "1":
        pytest.skip("Requires an explicitly selected disposable rootless workload cluster")
    audit = validate_rootless_boundary()
    problem = ProblemRegistry().get_problem_instance("node_clock_drift_hotel_reservation")
    assert isinstance(problem, TLSClockDriftHotelReservation)
    assert problem.task_version == "tls-validation-clock-v2"
    namespace = problem.namespace
    assert not problem.kubectl.exec_command_checked(
        f"kubectl get namespace {shlex.quote(namespace)} --ignore-not-found -o name"
    ).strip(), "Refusing to overwrite an existing application namespace"
    verifier = VerifierRuntime(kubeconfig_path=Path(filename), timeout_seconds=180)
    runner = ContainerRunner(
        ContainerConfig(
            kubeconfig_path=Path(filename),
            logs_path=tmp_path / "agent",
            codex_auth="none",
            forward_host_credentials=False,
            internet_policy=InternetPolicy.from_mode("open"),
        )
    )
    report = {"boundary": audit, "task_version": problem.task_version, "checks": {}}
    wall_before, monotonic_before = time.time(), time.monotonic()
    deployed = False

    def grade(label, expected):
        payload, handles = snapshot_oracle(problem.mitigation_oracle, Path(__file__).resolve().parents[2])
        result = verifier.evaluate_snapshot(payload, handles)
        report["checks"][label] = result
        (tmp_path / "task-qualification.json").write_text(json.dumps(report, indent=2))
        assert result.get("success") is expected, result
        return result

    def operator(command):
        result = runner.run_sync(ExecInput(command=command, label="tls-clock-operator", timeout=210))
        assert result.returncode == 0, result.stderr[-1500:]

    def patch_deployment(patch):
        operator(
            f"kubectl patch deployment/frontend -n {namespace} --type=strategic -p {shlex.quote(json.dumps(patch))}"
        )

    original_profile = {
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "containers": [tls_client_container()],
                    "volumes": tls_client_volumes(),
                }
            },
        }
    }
    try:
        verifier.prepare()
        deployed = True
        problem.app.deploy()
        problem.mitigation_oracle.capture_baseline()
        report["baseline_deployments"] = len(problem.mitigation_oracle.replica_count)
        grade("healthy", True)
        problem.inject_fault()  # Requires real certificate-expiry logs and loss of readiness.
        grade("injected_noop", False)
        operator(
            f"kubectl patch configmap/{CLOCK_CONFIG} -n {namespace} --type=merge "
            f"-p {shlex.quote(json.dumps({'data': {CLOCK_KEY: '0'}}))}"
        )
        operator(f"kubectl rollout status deployment/frontend -n {namespace} --timeout=180s")
        grade("reference_repair", True)

        patch_deployment(
            {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "tls-health-check",
                                    "readinessProbe": {"exec": {"command": ["true"]}},
                                }
                            ]
                        }
                    }
                }
            }
        )
        assert grade("bypass_readiness", False)["reason"] == "tls_validation_contract_changed"
        patch_deployment(original_profile)
        operator(f"kubectl rollout status deployment/frontend -n {namespace} --timeout=180s")

        patch_deployment(
            {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "tls-health-check",
                                    "$patch": "delete",
                                }
                            ]
                        }
                    }
                }
            }
        )
        assert grade("remove_tls_client", False)["reason"] == "tls_validation_contract_changed"
        patch_deployment(original_profile)
        operator(f"kubectl rollout status deployment/frontend -n {namespace} --timeout=180s")

        patch_deployment({"spec": {"replicas": 0}})
        assert grade("scale_frontend_to_zero", False)["reason"] == "required_deployment_scaled_to_zero"
        patch_deployment(original_profile)
        operator(f"kubectl rollout status deployment/frontend -n {namespace} --timeout=180s")
        grade("repair_after_adversarial_checks", True)
        wall_elapsed, monotonic_elapsed = time.time() - wall_before, time.monotonic() - monotonic_before
        report["host_clock_elapsed_difference_seconds"] = wall_elapsed - monotonic_elapsed
        assert abs(wall_elapsed - monotonic_elapsed) < 5
        report["passed"] = True
    finally:
        runner.close()
        verifier.cancel()
        if deployed:
            try:
                problem.recover_fault()
            finally:
                problem.app.cleanup()
        (tmp_path / "task-qualification.json").write_text(json.dumps(report, indent=2))
