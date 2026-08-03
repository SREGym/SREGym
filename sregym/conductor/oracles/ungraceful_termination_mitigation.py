"""
Mitigation oracle for the UngracefulPodTermination fault.

Passes when ALL of the following hold for the target deployment:

  1. terminationGracePeriodSeconds >= min_grace_period (default 30s)
  2. All pods in the namespace are Running and stable
  3. A controlled test rollout completes without dropping all replicas
     (verifies that pods can now drain properly before termination)
"""

import copy
import json
import os
import tempfile
import time

from sregym.conductor.oracles.base import Oracle


class UngracefulTerminationMitigationOracle(Oracle):
    rollout_timeout_seconds = 120
    poll_interval_seconds = 2
    stabilize_timeout_seconds = 60

    def __init__(self, problem, deployment_name: str, min_grace_period: int = 30):
        super().__init__(problem)
        self.deployment_name = deployment_name
        self.min_grace_period = min_grace_period
        self.namespace = problem.namespace
        self.kubectl = problem.kubectl

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_deployment_json(self) -> dict:
        output = self.kubectl.exec_command(
            f"kubectl get deployment {self.deployment_name} -n {self.namespace} -o json"
        )
        return json.loads(output)

    def _patch_deployment(self, patch: dict) -> None:
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                json.dump(patch, f)
                tmp_path = f.name
            self.kubectl.exec_command(
                f"kubectl patch deployment {self.deployment_name} -n {self.namespace} "
                f"--type=merge --patch-file {tmp_path}"
            )
        finally:
            if tmp_path is not None:
                os.unlink(tmp_path)

    def _rollout_complete(self, deployment: dict) -> bool:
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

    def _wait_for_rollout(
        self,
        minimum_generation: int,
        *,
        require_continuous_availability: bool,
    ) -> bool:
        deadline = time.monotonic() + self.rollout_timeout_seconds
        while time.monotonic() < deadline:
            deployment = self._get_deployment_json()
            generation = (deployment.get("metadata") or {}).get("generation", 0)
            available = (deployment.get("status") or {}).get("availableReplicas", 0)

            if require_continuous_availability and available < 1:
                print("❌ Deployment reached zero available replicas during test rollout")
                return False

            if generation >= minimum_generation and self._rollout_complete(deployment):
                return True

            time.sleep(self.poll_interval_seconds)

        print(f"❌ Timed out waiting for {self.deployment_name} rollout to complete")
        return False

    def _pods_stable(self) -> bool:
        try:
            pod_list = self.kubectl.list_pods(self.namespace)
            for pod in pod_list.items:
                if pod.status.phase != "Running":
                    return False
                for cs in pod.status.container_statuses or []:
                    if not cs.ready:
                        return False
                    if cs.state.waiting:
                        return False
            return True
        except Exception:
            return False

    def _apply_probe_annotation(self) -> None:
        """Touch the pod template to trigger a new rollout generation."""
        patch = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "oracle-drain-check": str(time.time_ns())
                        }
                    }
                }
            }
        }
        self._patch_deployment(patch)

    def _restore_probe_annotation(self, original_template: dict) -> None:
        """Remove the probe annotation after the test rollout."""
        template = copy.deepcopy(original_template)
        metadata = template.setdefault("metadata", {})
        annotations = metadata.get("annotations") or {}
        annotations["oracle-drain-check"] = None
        metadata["annotations"] = annotations
        self._patch_deployment({"spec": {"template": template}})

    # ------------------------------------------------------------------
    # Main evaluation
    # ------------------------------------------------------------------

    def evaluate(self) -> dict:
        print("== Ungraceful Termination Mitigation Evaluation ==")

        # --- Check 1: terminationGracePeriodSeconds ---
        try:
            deployment = self._get_deployment_json()
        except Exception as e:
            print(f"❌ Could not fetch deployment: {e}")
            return {"success": False}

        pod_spec = (deployment.get("spec") or {}).get("template", {}).get("spec") or {}
        grace_period = pod_spec.get("terminationGracePeriodSeconds", 30)

        if grace_period < self.min_grace_period:
            print(
                f"❌ terminationGracePeriodSeconds is {grace_period}s — "
                f"must be at least {self.min_grace_period}s to allow safe draining"
            )
            return {"success": False}

        print(f"✅ terminationGracePeriodSeconds={grace_period}s >= {self.min_grace_period}s")

        # --- Check 2: all pods Running before test rollout ---
        print(f"⏳ Waiting up to {self.stabilize_timeout_seconds}s for pods to stabilize...")
        deadline = time.monotonic() + self.stabilize_timeout_seconds
        while time.monotonic() < deadline:
            if self._pods_stable():
                break
            time.sleep(self.poll_interval_seconds)
        else:
            print("❌ Pods did not stabilize before test rollout")
            return {"success": False}

        initial_generation = (deployment.get("metadata") or {}).get("generation", 0)
        original_template = copy.deepcopy(
            (deployment.get("spec") or {}).get("template")
        )

        # --- Check 3: controlled test rollout stays available throughout ---
        probe_applied = False
        try:
            print("🔄 Triggering controlled test rollout to verify graceful drain...")
            self._apply_probe_annotation()
            probe_applied = True

            probe_deployment = self._get_deployment_json()
            probe_generation = (probe_deployment.get("metadata") or {}).get("generation", 0)

            if probe_generation <= initial_generation:
                print("❌ Test rollout did not increment deployment generation")
                return {"success": False}

            if not self._wait_for_rollout(
                probe_generation, require_continuous_availability=True
            ):
                print("❌ Test rollout dropped all replicas — pods are still not draining safely")
                return {"success": False}

            print("✅ Test rollout completed without dropping all replicas — pods drain safely")
            print("✅ Mitigation verified: ungraceful termination fault is resolved")
            return {"success": True}

        except Exception as e:
            print(f"❌ Error during test rollout: {e}")
            return {"success": False}

        finally:
            if probe_applied and original_template is not None:
                try:
                    self._restore_probe_annotation(original_template)
                except Exception as exc:
                    print(f"⚠️  Could not restore probe annotation: {exc}")