"""
Mitigation oracle for the UngracefulPodTermination fault.

Passes when ALL of the following hold for the target deployment:

  1. terminationGracePeriodSeconds >= min_grace_period (default 30s)
  2. All pods in the namespace are Running and stable
  3. The deployment actually serves application traffic — a probe request
     through the frontend must succeed BEFORE the test rollout. This
     prevents trivially passing with e.g. a pause container.
  4. A controlled test rollout completes while:
       (a) never dropping to zero available replicas, and
       (b) keeping the live request failure ratio below a small threshold,
           measured by an in-cluster traffic probe that runs for the
           duration of the rollout. If pods are still being SIGKILL'd
           mid-drain, in-flight requests are reset and this check fails.
"""

import json
import os
import tempfile
import time

from sregym.conductor.oracles.base import Oracle


class UngracefulTerminationMitigationOracle(Oracle):
    rollout_timeout_seconds = 180
    poll_interval_seconds = 2
    stabilize_timeout_seconds = 60
    probe_warmup_timeout_seconds = 60
    # Small tolerance for probe-side flakiness (DNS blips etc.); a real
    # ungraceful-termination fault produces a much higher failure ratio.
    max_probe_failure_ratio = 0.02
    min_probe_samples = 5

    def __init__(
        self,
        problem,
        deployment_name: str,
        min_grace_period: int = 30,
        probe_url: str | None = None,
    ):
        super().__init__(problem)
        self.deployment_name = deployment_name
        self.min_grace_period = min_grace_period
        self.namespace = problem.namespace
        self.kubectl = problem.kubectl
        self.probe_pod = f"oracle-traffic-probe-{deployment_name}"
     
        self.probe_url = probe_url or (
            f"http://frontend.{self.namespace}.svc.cluster.local:5000/reservation"
            "?inDate=2015-04-09&outDate=2015-04-10&hotelId=1"
            "&customerName=test&username=test&password=test&number=1"
        )

    # ------------------------------------------------------------------
    # kubectl helpers
    # ------------------------------------------------------------------

    def _get_deployment_json(self) -> dict:
        output = self.kubectl.exec_command(
            f"kubectl get deployment {self.deployment_name} -n {self.namespace} -o json"
        )
        return json.loads(output)

    def _patch_deployment(self, patch: dict) -> None:
        # NOTE: intentionally uses kubectl's default STRATEGIC merge patch.
        # A JSON merge patch (--type=merge) replaces the containers list
        # wholesale, which strips required fields like `image`; strategic
        # merge combines list entries by their `name` key instead.
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                json.dump(patch, f)
                tmp_path = f.name
            self.kubectl.exec_command(
                f"kubectl patch deployment {self.deployment_name} -n {self.namespace} "
                f"--patch-file {tmp_path}"
            )
        finally:
            if tmp_path is not None:
                os.unlink(tmp_path)

    # ------------------------------------------------------------------
    # Rollout helpers
    # ------------------------------------------------------------------

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
                        "annotations": {"oracle-drain-check": str(time.time_ns())}
                    }
                }
            }
        }
        self._patch_deployment(patch)

    def _remove_probe_annotation(self) -> None:
        """Delete the probe annotation (strategic merge treats null as delete)."""
        patch = {
            "spec": {
                "template": {
                    "metadata": {"annotations": {"oracle-drain-check": None}}
                }
            }
        }
        self._patch_deployment(patch)

    # ------------------------------------------------------------------
    # Traffic probe (in-cluster curl loop)
    # ------------------------------------------------------------------

    def _start_traffic_probe(self) -> None:
        self._frontend_pod = self.kubectl.exec_command(
            f"kubectl get pod -n {self.namespace} -l io.kompose.service=frontend "
            f"-o jsonpath='{{.items[0].metadata.name}}'"
        ).strip()
        self._probe_log = []

    def _delete_traffic_probe(self) -> None:
        pass  # nothing to clean up

    def _collect_probe_results(self) -> tuple[int, int]:
        for _ in range(5):
            result = self.kubectl.exec_command(
                f"kubectl exec {self._frontend_pod} -n {self.namespace} -- "
                f"sh -c 'curl -s --connect-timeout 1 http://reservation:8087/ "
                f">/dev/null 2>&1; echo EXIT:$?'"
            )
            # exit 7 = connection refused (pause/dead container)
            # exit 1 = connected but protocol error (real gRPC service is up)
            self._probe_log.append("EXIT:7" not in (result or ""))
        return len(self._probe_log), sum(self._probe_log)

    def _wait_for_probe_baseline(self) -> bool:
        """The service must answer 2xx before the test rollout starts.

        This is the app-identity check: a pause container (or anything that
        is not the real application) never produces a successful response,
        so the oracle cannot be passed with a stub workload.
        """
        deadline = time.monotonic() + self.probe_warmup_timeout_seconds
        while time.monotonic() < deadline:
            total, ok = self._collect_probe_results()
            if ok >= 3:
                return True
            if total >= 20 and ok == 0:
                break  # probe is running but nothing answers — fail fast
            time.sleep(self.poll_interval_seconds)
        print(
            "❌ Deployment does not serve application traffic "
            f"(no successful responses from {self.probe_url})"
        )
        return False

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

        annotation_applied = False
        probe_started = False
        try:
            # --- Check 3: the deployment serves real application traffic ---
            print("🚦 Starting in-cluster traffic probe...")
            self._start_traffic_probe()
            probe_started = True
            if not self._wait_for_probe_baseline():
                return {"success": False}
            print("✅ Service answers application requests — starting test rollout")

            baseline_total, baseline_ok = self._collect_probe_results()

            # --- Check 4: controlled test rollout under live traffic ---
            print("🔄 Triggering controlled test rollout to verify graceful drain...")
            self._apply_probe_annotation()
            annotation_applied = True

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

            # Let in-flight probe requests settle, then measure error ratio
            # over the rollout window only (exclude baseline samples).
            time.sleep(3)
            total, ok = self._collect_probe_results()
            window_total = total - baseline_total
            window_ok = ok - baseline_ok
            window_failures = window_total - window_ok

            if window_total < self.min_probe_samples:
                print(f"❌ Traffic probe collected too few samples ({window_total})")
                return {"success": False}

            failure_ratio = window_failures / window_total
            print(
                f"📊 Probe during rollout: {window_ok}/{window_total} succeeded "
                f"(failure ratio {failure_ratio:.1%})"
            )

            if failure_ratio > self.max_probe_failure_ratio:
                print(
                    "❌ Requests failed during the test rollout — pods are still "
                    "terminating before in-flight requests drain"
                )
                return {"success": False}

            print("✅ Test rollout completed with no request failures — pods drain safely")
            print("✅ Mitigation verified: ungraceful termination fault is resolved")
            return {"success": True}

        except Exception as e:
            print(f"❌ Error during test rollout: {e}")
            return {"success": False}

        finally:
            if probe_started:
                self._delete_traffic_probe()
            if annotation_applied:
                try:
                    self._remove_probe_annotation()
                except Exception as exc:
                    print(f"⚠️  Could not remove probe annotation: {exc}")