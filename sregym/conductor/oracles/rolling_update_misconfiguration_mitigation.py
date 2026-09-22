import copy
import json
import math
import os
import tempfile
import time

import yaml

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.service.rollout import deployment_rollout_complete


class RollingUpdateMitigationOracle(Oracle):
    rollout_timeout_seconds = 120
    poll_interval_seconds = 2

    FAILURE_CLASSES = {
        # The agent edits this very Deployment as its mitigation, so it sits
        # inside the problem's blast radius. A stalled rollout of a Deployment
        # the agent just rewrote is not attributable to infrastructure the way
        # it is elsewhere -- hence the override of the shared
        # ENVIRONMENT_ERROR.
        "required_deployment_not_rolled_out": FailureClass.AMBIGUOUS,
        # Losing every replica mid-rollout is the injected fault's whole
        # symptom, observed under a rollout this oracle triggers itself. It is
        # the decisive check.
        "lost_all_available_replicas": FailureClass.AGENT_ERROR,
        # We could not read or drive the Deployment: our probe, our problem.
        "deployment_yaml_unreadable": FailureClass.ENVIRONMENT_ERROR,
        "deployment_has_no_pod_template": FailureClass.AMBIGUOUS,
        "rollout_probe_ineffective": FailureClass.HARNESS_ERROR,
    }

    def __init__(self, problem, deployment_name: str):
        super().__init__(problem)
        self.deployment_name = deployment_name
        self.namespace = problem.namespace
        self.kubectl = problem.kubectl

    @staticmethod
    def _scaled_int_or_percent(value, replicas: int, *, round_up: bool) -> int:
        if isinstance(value, int):
            return value
        if not isinstance(value, str):
            raise ValueError(f"Unsupported rolling update value: {value!r}")
        if not value.endswith("%"):
            return int(value)

        percentage = int(value[:-1])
        scaled = replicas * percentage / 100
        return math.ceil(scaled) if round_up else math.floor(scaled)

    @classmethod
    def _strategy_preserves_availability(cls, deployment: dict) -> bool:
        spec = deployment.get("spec") or {}
        replicas = spec.get("replicas", 1)
        if not isinstance(replicas, int) or replicas < 1:
            return False

        strategy = spec.get("strategy") or {}
        if strategy.get("type", "RollingUpdate") != "RollingUpdate":
            return False

        rolling_update = strategy.get("rollingUpdate") or {}
        try:
            max_unavailable = cls._scaled_int_or_percent(
                rolling_update.get("maxUnavailable", "25%"),
                replicas,
                round_up=False,
            )
            max_surge = cls._scaled_int_or_percent(
                rolling_update.get("maxSurge", "25%"),
                replicas,
                round_up=True,
            )
        except (TypeError, ValueError):
            return False

        return 0 <= max_unavailable < replicas and max_surge >= 0 and (max_unavailable > 0 or max_surge > 0)

    def _get_deployment_json(self) -> dict:
        output = self.kubectl.exec_command(f"kubectl get deployment {self.deployment_name} -n {self.namespace} -o json")
        return json.loads(output)

    @staticmethod
    def _rollout_complete(deployment: dict) -> bool:
        return deployment_rollout_complete(deployment)

    def _rollout_failed(self, minimum_generation: int, *, require_continuous_availability: bool) -> dict | None:
        """Return a verdict if the rollout did not complete cleanly, else None.

        The two failures here were previously one bare ``False``, which is the
        worst possible conflation for this problem: dropping to zero available
        replicas *is* the fault under test, while a timeout is a much weaker
        signal. They now report separately.
        """
        deadline = time.monotonic() + self.rollout_timeout_seconds
        while time.monotonic() < deadline:
            deployment = self._get_deployment_json()
            generation = (deployment.get("metadata") or {}).get("generation", 0)
            available = (deployment.get("status") or {}).get("availableReplicas", 0)

            if require_continuous_availability and available < 1:
                print("❌ Mitigation failed: deployment reached zero available replicas")
                return self.fail("lost_all_available_replicas", deployment=self.deployment_name)

            if generation >= minimum_generation and self._rollout_complete(deployment):
                return None

            time.sleep(self.poll_interval_seconds)

        print(f"❌ Timed out waiting for deployment/{self.deployment_name} rollout")
        return self.fail(
            "required_deployment_not_rolled_out",
            deployment=self.deployment_name,
            waited_seconds=self.rollout_timeout_seconds,
        )

    def _patch_deployment(self, patch: dict) -> str:
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tmp:
                yaml.safe_dump(patch, tmp)
                tmp_path = tmp.name
            return self.kubectl.exec_command(
                f"kubectl patch deployment {self.deployment_name} -n {self.namespace} "
                f"--type=merge --patch-file {tmp_path}"
            )
        finally:
            if tmp_path is not None:
                os.unlink(tmp_path)

    def _apply_rollout_probe(self) -> None:
        probe_patch = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {"rollout-readiness-check": str(time.time_ns())},
                    },
                    "spec": {
                        "initContainers": [
                            {
                                "name": "hang-init",
                                "image": "busybox:1.36",
                                "imagePullPolicy": "IfNotPresent",
                                "command": ["/bin/sh", "-c", "sleep 15"],
                            }
                        ]
                    },
                }
            }
        }

        output = self._patch_deployment(probe_patch)
        print(f"Patched rollout probe: {output}")

    def _restore_pod_template(self, original_template: dict) -> None:
        template = copy.deepcopy(original_template)

        metadata = template.setdefault("metadata", {})
        annotations = metadata.get("annotations") or {}
        if "rollout-readiness-check" not in annotations:
            annotations["rollout-readiness-check"] = None
        metadata["annotations"] = annotations

        pod_spec = template.setdefault("spec", {})
        if "initContainers" not in pod_spec:
            pod_spec["initContainers"] = None

        output = self._patch_deployment({"spec": {"template": template}})
        print(f"Restored repaired pod template: {output}")

    def evaluate(self) -> dict:
        print("== Rolling Update Mitigation Evaluation ==")

        original_template = None
        probe_applied = False
        try:
            output = self.kubectl.exec_command(
                f"kubectl get deployment {self.deployment_name} -n {self.namespace} -o yaml"
            )
            deployment = yaml.safe_load(output)
            if not isinstance(deployment, dict):
                print("❌ Mitigation failed: deployment output was not valid YAML")
                return self.fail("deployment_yaml_unreadable", deployment=self.deployment_name)

            original_template = copy.deepcopy((deployment.get("spec") or {}).get("template"))
            if not isinstance(original_template, dict):
                print("❌ Mitigation failed: deployment has no pod template")
                return self.fail("deployment_has_no_pod_template", deployment=self.deployment_name)

            if not self._strategy_preserves_availability(deployment):
                print("❌ Mitigation failed: rolling update strategy permits total unavailability")
                # The injected fault restated: the strategy still allows every
                # replica to go away at once.
                return self.fail("fault_still_present", deployment=self.deployment_name)

            initial_generation = (deployment.get("metadata") or {}).get("generation", 0)
            not_ready = self._rollout_failed(initial_generation, require_continuous_availability=False)
            if not_ready is not None:
                print("❌ Mitigation failed: repaired deployment did not become ready")
                return not_ready

            print("🔄 Triggering controlled slow rollout")
            self._apply_rollout_probe()
            probe_applied = True
            probe_deployment = self._get_deployment_json()
            probe_generation = (probe_deployment.get("metadata") or {}).get("generation", 0)
            if probe_generation <= initial_generation:
                print("❌ Mitigation failed: rollout probe did not update the deployment generation")
                # Our probe patch did not take effect, so we never ran the test
                # this oracle exists to run.
                return self.fail("rollout_probe_ineffective", deployment=self.deployment_name)

            failed = self._rollout_failed(probe_generation, require_continuous_availability=True)
            if failed is not None:
                return failed

            print("✅ Mitigation successful: rollout completed without losing all replicas")
            return {"success": True}
        except Exception as e:
            print(f"❌ Error during evaluation: {e}")
            return self.fail_from_exception(e, deployment=self.deployment_name)
        finally:
            if probe_applied and original_template is not None:
                try:
                    self._restore_pod_template(original_template)
                except Exception as exc:
                    print(f"⚠️ Failed to restore repaired pod template: {exc}")
