"""
Ungraceful Pod Termination — Discord March 2026 voice outage simulation.

Real-world story:
  On March 25, 2026, Discord suffered a 3-hour voice outage after a routine
  Kubernetes scale-down terminated 50% of session pods simultaneously.
  A safety check meant to wait for in-flight handoffs to complete caused the
  terminationGracePeriodSeconds to elapse before handoffs could begin.
  Kubernetes sent SIGKILL, dropping 17% of active sessions and cascading
  into downstream voice syncer failure.

  Reference: https://discord.com/blog/behind-the-scenes-of-the-3-25-26-voice-outage

Simulation:
  Patch the `reservation` deployment with terminationGracePeriodSeconds=1
  and a preStop hook that sleeps 30 seconds (simulating Discord's handoff
  process). Then trigger a rolling restart so the fault is immediately
  observable — pods are SIGKILL'd before the preStop hook can complete,
  dropping in-flight reservation requests. The mitigation oracle then
  performs a controlled test rollout to verify that the fix allows pods
  to drain properly before termination.
"""

import json
import os
import tempfile
import time

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.ungraceful_termination_mitigation import UngracefulTerminationMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

# Generic names — do not expose fault category to agent
_PRESLEEP_SECONDS = 30
_FAULT_GRACE_PERIOD = 1


class UngracefulPodTermination(Problem):
    def __init__(self, app_name: str = "hotel_reservation"):
        self.faulty_service = "reservation"

        if app_name == "hotel_reservation":
            app = HotelReservation()
        else:
            raise ValueError(f"Unsupported app name: {app_name}")

        super().__init__(app=app)

        self.kubectl = KubeCtl()
        self._original_grace_period = 30
        self._original_lifecycle = None
        self._container_name = None

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"The `{self.faulty_service}` deployment has terminationGracePeriodSeconds "
                f"set to {_FAULT_GRACE_PERIOD}, far shorter than the {_PRESLEEP_SECONDS}s "
                "required for its preStop hook to complete connection draining. "
                "When pods restart, Kubernetes sends SIGKILL before the preStop hook "
                "finishes, dropping all in-flight requests abruptly. This produces "
                "connection-reset errors in dependent services and elevated 5xx rates "
                "visible in logs and metrics during any pod churn event."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.app.create_workload()
        self.mitigation_oracle = UngracefulTerminationMitigationOracle(
            problem=self,
            deployment_name=self.faulty_service,
            min_grace_period=_PRESLEEP_SECONDS,
        )

    def _get_deployment_json(self) -> dict:
        output = self.kubectl.exec_command(
            f"kubectl get deployment {self.faulty_service} -n {self.namespace} -o json"
        )
        return json.loads(output)

    def _patch_deployment(self, patch: dict) -> None:
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                json.dump(patch, f)
                tmp_path = f.name
            self.kubectl.exec_command(
                f"kubectl patch deployment {self.faulty_service} -n {self.namespace} "
                f"--type=merge --patch-file {tmp_path}"
            )
        finally:
            if tmp_path is not None:
                os.unlink(tmp_path)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")

        # Save original deployment state
        deployment = self._get_deployment_json()
        pod_spec = deployment["spec"]["template"]["spec"]
        self._original_grace_period = pod_spec.get("terminationGracePeriodSeconds", 30)
        self._container_name = pod_spec["containers"][0]["name"]
        self._original_lifecycle = pod_spec["containers"][0].get("lifecycle")

        # Inject: short grace period + slow preStop hook
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "terminationGracePeriodSeconds": _FAULT_GRACE_PERIOD,
                        "containers": [
                            {
                                "name": self._container_name,
                                "lifecycle": {
                                    "preStop": {
                                        "exec": {
                                            "command": [
                                                "/bin/sh",
                                                "-c",
                                                f"sleep {_PRESLEEP_SECONDS}",
                                            ]
                                        }
                                    }
                                },
                            }
                        ],
                    }
                }
            }
        }
        self._patch_deployment(patch)

        # Trigger rolling restart to make the fault immediately observable
        self.kubectl.exec_command(
            f"kubectl rollout restart deployment/{self.faulty_service} -n {self.namespace}"
        )

        print(
            f"Injected ungraceful termination fault on: {self.faulty_service} "
            f"(terminationGracePeriodSeconds={_FAULT_GRACE_PERIOD}, "
            f"preStop sleep={_PRESLEEP_SECONDS}s)"
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")

        container_name = self._container_name
        if container_name is None:
            deployment = self._get_deployment_json()
            container_name = deployment["spec"]["template"]["spec"]["containers"][0]["name"]

        # Restore original terminationGracePeriodSeconds and remove preStop hook
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "terminationGracePeriodSeconds": self._original_grace_period,
                        "containers": [
                            {
                                "name": container_name,
                                "lifecycle": self._original_lifecycle,
                            }
                        ],
                    }
                }
            }
        }
        self._patch_deployment(patch)

        # Wait for rollout to settle
        self.kubectl.exec_command(
            f"kubectl rollout status deployment/{self.faulty_service} "
            f"-n {self.namespace} --timeout=120s"
        )

        print(f"Recovered ungraceful termination fault on: {self.faulty_service}")
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}")