import copy
import json
import math
import os
import tempfile
import time

import yaml

from sregym.conductor.oracles.base import Oracle


class RollingUpdateMitigationOracle(Oracle):
    rollout_timeout_seconds = 120
    poll_interval_seconds = 2

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
        metadata = deployment.get("metadata") or {}
        spec = deployment.get("spec") or {}
        status = deployment.get("status") or {}
        generation = metadata.get("generation", 0)
        replicas = spec.get("replicas", 1)

        return (
            status.get("observedGeneration", 0) >= generation
            and status.get("updatedReplicas", 0) == replicas
            and status.get("readyReplicas", 0) == replicas
            and status.get("availableReplicas", 0) == replicas
            and status.get("unavailableReplicas", 0) == 0
        )

    def _wait_for_rollout(self, minimum_generation: int, *, require_continuous_availability: bool) -> bool:
        deadline = time.monotonic() + self.rollout_timeout_seconds
        while time.monotonic() < deadline:
            deployment = self._get_deployment_json()
            generation = (deployment.get("metadata") or {}).get("generation", 0)
            available = (deployment.get("status") or {}).get("availableReplicas", 0)

            if require_continuous_availability and available < 1:
                print("❌ Mitigation failed: deployment reached zero available replicas")
                return False

            if generation >= minimum_generation and self._rollout_complete(deployment):
                return True

            time.sleep(self.poll_interval_seconds)

        print(f"❌ Timed out waiting for deployment/{self.deployment_name} rollout")
        return False

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
                return {"success": False}

            original_template = copy.deepcopy((deployment.get("spec") or {}).get("template"))
            if not isinstance(original_template, dict):
                print("❌ Mitigation failed: deployment has no pod template")
                return {"success": False}

            if not self._strategy_preserves_availability(deployment):
                print("❌ Mitigation failed: rolling update strategy permits total unavailability")
                return {"success": False}

            initial_generation = (deployment.get("metadata") or {}).get("generation", 0)
            if not self._wait_for_rollout(initial_generation, require_continuous_availability=False):
                print("❌ Mitigation failed: repaired deployment did not become ready")
                return {"success": False}

            print("🔄 Triggering controlled slow rollout")
            self._apply_rollout_probe()
            probe_applied = True
            probe_deployment = self._get_deployment_json()
            probe_generation = (probe_deployment.get("metadata") or {}).get("generation", 0)
            if probe_generation <= initial_generation:
                print("❌ Mitigation failed: rollout probe did not update the deployment generation")
                return {"success": False}

            if not self._wait_for_rollout(probe_generation, require_continuous_availability=True):
                return {"success": False}

            print("✅ Mitigation successful: rollout completed without losing all replicas")
            return {"success": True}
        except Exception as e:
            print(f"❌ Error during evaluation: {e}")
            return {"success": False}
        finally:
            if probe_applied and original_template is not None:
                try:
                    self._restore_pod_template(original_template)
                except Exception as exc:
                    print(f"⚠️ Failed to restore repaired pod template: {exc}")
