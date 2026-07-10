import json
from types import SimpleNamespace

import yaml

import sregym.conductor.oracles.rolling_update_misconfiguration_mitigation as rolling_update_module
from sregym.conductor.oracles.rolling_update_misconfiguration_mitigation import RollingUpdateMitigationOracle


def _deployment(
    *,
    generation=1,
    observed_generation=1,
    replicas=3,
    updated=3,
    ready=3,
    available=3,
    unavailable=0,
    max_unavailable="25%",
    max_surge="25%",
):
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "custom-service", "generation": generation},
        "spec": {
            "replicas": replicas,
            "template": {
                "metadata": {"labels": {"app": "custom-service"}},
                "spec": {
                    "containers": [
                        {
                            "name": "custom-service-main",
                            "image": "python:3.9-slim",
                        }
                    ]
                },
            },
            "strategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {
                    "maxUnavailable": max_unavailable,
                    "maxSurge": max_surge,
                },
            },
        },
        "status": {
            "observedGeneration": observed_generation,
            "updatedReplicas": updated,
            "readyReplicas": ready,
            "availableReplicas": available,
            "unavailableReplicas": unavailable,
        },
    }


class _KubeCtl:
    def __init__(self, initial_deployment, json_deployments):
        self.initial_deployment = initial_deployment
        self.json_deployments = list(json_deployments)
        self.patch_commands = []
        self.patches = []

    def exec_command(self, command):
        if command.endswith("-o yaml"):
            return yaml.safe_dump(self.initial_deployment)
        if command.endswith("-o json"):
            if not self.json_deployments:
                raise AssertionError("No deployment status queued")
            return json.dumps(self.json_deployments.pop(0))
        if "--patch-file" in command:
            self.patch_commands.append(command)
            patch_path = command.split("--patch-file ", 1)[1]
            with open(patch_path) as patch_file:
                self.patches.append(yaml.safe_load(patch_file))
            return "deployment.apps/custom-service patched\n"
        raise AssertionError(f"Unexpected command: {command}")


def _oracle(kubectl):
    problem = SimpleNamespace(namespace="social-network", kubectl=kubectl)
    oracle = RollingUpdateMitigationOracle(problem, "custom-service")
    oracle.poll_interval_seconds = 0
    return oracle


def test_rejects_original_strategy_without_triggering_probe():
    deployment = _deployment(max_unavailable="100%", max_surge="0%")
    kubectl = _KubeCtl(deployment, [])

    assert _oracle(kubectl).evaluate()["success"] is False
    assert kubectl.patch_commands == []


def test_accepts_safe_strategy_after_stable_probe_rollout(monkeypatch):
    monkeypatch.setattr(rolling_update_module.time, "time_ns", lambda: 1234)
    initial = _deployment()
    probe_started = _deployment(
        generation=2,
        observed_generation=1,
        updated=0,
        ready=3,
        available=3,
    )
    probe_progress = _deployment(
        generation=2,
        observed_generation=2,
        updated=1,
        ready=3,
        available=3,
    )
    probe_complete = _deployment(generation=2, observed_generation=2)
    kubectl = _KubeCtl(
        initial,
        [
            initial,
            probe_started,
            probe_progress,
            probe_complete,
        ],
    )

    assert _oracle(kubectl).evaluate()["success"] is True
    assert len(kubectl.patch_commands) == 2
    probe_container = kubectl.patches[0]["spec"]["template"]["spec"]["initContainers"][0]
    assert probe_container["image"] == "busybox:1.36"
    assert probe_container["imagePullPolicy"] == "IfNotPresent"
    restored_template = kubectl.patches[1]["spec"]["template"]
    assert restored_template["spec"]["initContainers"] is None
    assert restored_template["metadata"]["annotations"]["rollout-readiness-check"] is None


def test_rejects_zero_availability_during_probe(monkeypatch):
    monkeypatch.setattr(rolling_update_module.time, "time_ns", lambda: 1234)
    initial = _deployment()
    probe_started = _deployment(
        generation=2,
        observed_generation=1,
        updated=0,
        ready=3,
        available=3,
    )
    outage = _deployment(
        generation=2,
        observed_generation=2,
        updated=0,
        ready=0,
        available=0,
        unavailable=3,
    )
    kubectl = _KubeCtl(initial, [initial, probe_started, outage])

    assert _oracle(kubectl).evaluate()["success"] is False
    assert len(kubectl.patch_commands) == 2


def test_rejects_probe_that_does_not_advance_generation(monkeypatch):
    monkeypatch.setattr(rolling_update_module.time, "time_ns", lambda: 1234)
    initial = _deployment()
    kubectl = _KubeCtl(initial, [initial, initial])

    assert _oracle(kubectl).evaluate()["success"] is False
    assert len(kubectl.patch_commands) == 2


def test_restore_preserves_the_repaired_template():
    original_template = _deployment()["spec"]["template"]
    original_template["metadata"]["annotations"] = {"agent.example/repair": "kept"}
    original_template["spec"]["initContainers"] = [
        {
            "name": "agent-setup",
            "image": "busybox:1.36",
            "command": ["sh", "-c", "true"],
        }
    ]
    kubectl = _KubeCtl(_deployment(), [])

    _oracle(kubectl)._restore_pod_template(original_template)

    restored_template = kubectl.patches[0]["spec"]["template"]
    assert restored_template["metadata"]["annotations"]["agent.example/repair"] == "kept"
    assert restored_template["metadata"]["annotations"]["rollout-readiness-check"] is None
    assert restored_template["spec"]["initContainers"] == original_template["spec"]["initContainers"]
