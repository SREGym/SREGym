"""
Mitigation oracle for the UngracefulPodTermination fault.

This oracle is BEHAVIORAL: it does not require any specific fix. Any
mitigation that makes pod termination actually graceful passes; anything
that still results in pods being SIGKILL'd before they finish draining
fails.

Passes when ALL of the following hold for the target deployment:

  1. All pods in the namespace are Running and stable.

  2. App-identity baseline: the deployment must serve real application
     traffic through the frontend (HTTP 200 on /hotels, which exercises
     reservation.CheckAvailability) BEFORE the test rollout. This check
     catches pause containers, crash-looping pods, wrong images, and any
     stub that is not the real application — all of which would otherwise
     produce ambiguous results in the termination-timing check below.

  3. Termination-duration check: a controlled test rollout (1 replica, so
     the replacement sequence is unambiguous) is triggered, and the old
     pod's actual termination duration is measured — from the moment its
     deletionTimestamp is set to the moment the pod object disappears.

     - If the pod is SIGKILL'd (grace period too short), it disappears in
       roughly 1–3 s.
     - If the drain/handoff completes before SIGKILL, the pod lives for
       approximately the drain duration before exiting cleanly.

     The oracle fails if the measured duration is below min_drain_seconds
     (default: _HANDOFF_SECONDS - 10 = 35 s). This is purely a timing
     measurement against the pod lifecycle API — no log buffers, no
     endpoint routing, no HTTP probes during the termination gap — so it
     is reliable even on fast clusters where pod objects are GC'd quickly.

  Rationale for using termination duration rather than HTTP probes during
  the rollout: Kubernetes removes the pod from service Endpoints before
  (or simultaneously with) delivering SIGTERM. As a result, the
  load-balancer routes all new requests to surviving replicas before the
  pod starts draining; an HTTP probe sees no failures regardless of
  whether the pod was SIGKILL'd or drained cleanly. Termination duration
  is the only signal that is both reliable and discriminating.
"""

import json
import os
import tempfile
import threading
import time

from sregym.conductor.oracles.base import Oracle


class UngracefulTerminationMitigationOracle(Oracle):
    rollout_timeout_seconds = 240
    poll_interval_seconds = 2
    stabilize_timeout_seconds = 180
    baseline_timeout_seconds = 60
    baseline_required_successes = 5
    probe_interval_seconds = 0.25
    # How long to wait for the old pod to enter Terminating state after
    # the rollout annotation is applied.
    terminating_wait_seconds = 60
    # How long to wait for the pod to disappear after entering Terminating.
    disappear_wait_seconds = 300

    def __init__(
        self,
        problem,
        deployment_name: str,
        frontend_label: str = "io.kompose.service=frontend",
        frontend_port: int = 5000,
        probe_path: str = (
            "/hotels?inDate=2015-04-09&outDate=2015-04-10"
            "&lat=38.0235&lon=-122.0936"
        ),
        # Minimum termination duration (seconds) that indicates the drain
        # completed before SIGKILL. Default is 35s — well above the ~2s
        # SIGKILL-case and well below the 45s handoff window.
        min_drain_seconds: float = 35.0,
        churn_cronjob: str | None = None,
    ):
        super().__init__(problem)
        self.deployment_name = deployment_name
        self.namespace = problem.namespace
        self.kubectl = problem.kubectl
        self.frontend_label = frontend_label
        self.frontend_port = frontend_port
        # /hotels exercises reservation.CheckAvailability through the real
        # application path (frontend → consul → reservation).
        self.probe_path = probe_path
        self.min_drain_seconds = min_drain_seconds
        self.churn_cronjob = churn_cronjob

        self._frontend_pod: str | None = None
        self._samples: list[tuple[float, bool]] = []
        self._samples_lock = threading.Lock()
        self._stop_probe = threading.Event()
        self._probe_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # kubectl helpers
    # ------------------------------------------------------------------

    def _get_deployment_json(self) -> dict:
        output = self.kubectl.exec_command(
            f"kubectl get deployment {self.deployment_name} -n {self.namespace} -o json"
        )
        return json.loads(output)

    def _patch_deployment(self, patch: dict) -> None:
        # Strategic merge patch (kubectl default): merges containers by
        # `name` instead of replacing the list wholesale.
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

    def _set_churn_suspended(self, suspended: bool) -> None:
        if not self.churn_cronjob:
            return
        try:
            value = "true" if suspended else "false"
            self.kubectl.exec_command(
                f"kubectl patch cronjob {self.churn_cronjob} -n {self.namespace} "
                f'-p \'{{"spec":{{"suspend":{value}}}}}\''
            )

            # Verify the patch actually landed — exec_command silently
            # swallows kubectl errors, so re-fetch and confirm spec.suspend.
            if suspended:
                actual = self.kubectl.exec_command(
                    f"kubectl get cronjob {self.churn_cronjob} -n {self.namespace} "
                    f"-o jsonpath='{{.spec.suspend}}' 2>/dev/null || true"
                ).strip()
                if actual != "true":
                    print(
                        f"⚠️  Suspend patch did not land on {self.churn_cronjob} "
                        f"(spec.suspend={actual!r}) — churn may fire during evaluation"
                    )
                    return

                # Wait for any already-running Job to finish.
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    active = self.kubectl.exec_command(
                        f"kubectl get cronjob {self.churn_cronjob} -n {self.namespace} "
                        f"-o jsonpath='{{.status.active}}' 2>/dev/null || true"
                    ).strip()
                    if not active or active in ("null", "[]"):
                        break
                    time.sleep(2)
                else:
                    print(
                        f"⚠️  Churn job still active after 120s — proceeding anyway, "
                        "but a concurrent pod deletion may affect evaluation results"
                    )
        except Exception:
            pass
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

    def _wait_for_rollout(self, minimum_generation: int) -> bool:
        deadline = time.monotonic() + self.rollout_timeout_seconds
        while time.monotonic() < deadline:
            deployment = self._get_deployment_json()
            generation = (deployment.get("metadata") or {}).get("generation", 0)
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

    def _apply_rollout_annotation(self) -> None:
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

    def _remove_rollout_annotation(self) -> None:
        patch = {
            "spec": {
                "template": {
                    "metadata": {"annotations": {"oracle-drain-check": None}}
                }
            }
        }
        self._patch_deployment(patch)

    # ------------------------------------------------------------------
    # Termination duration measurement
    # ------------------------------------------------------------------

    def _current_pod_name(self) -> str | None:
        """Return the name of a Running pod for the target deployment."""
        out = self.kubectl.exec_command(
            f"kubectl get pod -n {self.namespace} "
            f"-l io.kompose.service={self.deployment_name} "
            f"--field-selector=status.phase=Running "
            f"-o jsonpath='{{.items[0].metadata.name}}'"
        ).strip()
        return out if out and not out.startswith("error") else None

    def _pod_exists(self, pod_name: str) -> bool:
        out = self.kubectl.exec_command(
            f"kubectl get pod {pod_name} -n {self.namespace} --no-headers 2>/dev/null || true"
        )
        return bool(out and "NotFound" not in out)

    def _pod_is_terminating(self, pod_name: str) -> bool:
        out = self.kubectl.exec_command(
            f"kubectl get pod {pod_name} -n {self.namespace} --no-headers 2>/dev/null || true"
        )
        return bool(out) and "Terminating" in out

    def _measure_termination_duration(self, pod_name: str) -> float | None:
        """Measure seconds from pod entering Terminating to pod disappearing.

        Returns None if the pod never enters Terminating within
        terminating_wait_seconds, or never disappears within
        disappear_wait_seconds. Both are genuine failures.
        """
        # Wait for pod to enter Terminating state
        deadline = time.monotonic() + self.terminating_wait_seconds
        while time.monotonic() < deadline:
            if not self._pod_exists(pod_name):
                # Pod already gone — record near-zero duration
                return 0.0
            if self._pod_is_terminating(pod_name):
                break
            time.sleep(0.5)
        else:
            print(
                f"❌ Pod {pod_name} did not enter Terminating within "
                f"{self.terminating_wait_seconds}s"
            )
            return None

        terminating_at = time.monotonic()

        # Wait for pod to disappear
        deadline = time.monotonic() + self.disappear_wait_seconds
        while time.monotonic() < deadline:
            if not self._pod_exists(pod_name):
                return time.monotonic() - terminating_at
            time.sleep(0.5)

        print(
            f"❌ Pod {pod_name} did not disappear within "
            f"{self.disappear_wait_seconds}s after entering Terminating"
        )
        return None

    # ------------------------------------------------------------------
    # Baseline traffic probe (app-identity check only)
    # ------------------------------------------------------------------

    def _resolve_frontend_pod(self) -> bool:
        try:
            name = self.kubectl.exec_command(
                f"kubectl get pod -n {self.namespace} -l {self.frontend_label} "
                f"--field-selector=status.phase=Running "
                f"-o jsonpath='{{.items[0].metadata.name}}'"
            ).strip()
        except Exception:
            name = ""
        if not name or name.startswith("error"):
            print("❌ Could not resolve a running frontend pod for the baseline probe")
            return False
        self._frontend_pod = name
        return True

    def _probe_once(self) -> bool:
        """Strict: only exact '200' counts."""
        try:
            out = self.kubectl.exec_command(
                f"kubectl exec {self._frontend_pod} -n {self.namespace} -- "
                f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 2 "
                f"'http://localhost:{self.frontend_port}{self.probe_path}'"
            )
        except Exception:
            return False
        return (out or "").strip() == "200"

    def _probe_loop(self) -> None:
        while not self._stop_probe.is_set():
            ok = self._probe_once()
            with self._samples_lock:
                self._samples.append((time.monotonic(), ok))
            self._stop_probe.wait(self.probe_interval_seconds)

    def _start_probe(self) -> None:
        self._samples = []
        self._stop_probe.clear()
        self._probe_thread = threading.Thread(target=self._probe_loop, daemon=True)
        self._probe_thread.start()

    def _stop_probe_thread(self) -> None:
        self._stop_probe.set()
        if self._probe_thread is not None:
            self._probe_thread.join(timeout=10)
            self._probe_thread = None

    def _samples_snapshot(self) -> list[tuple[float, bool]]:
        with self._samples_lock:
            return list(self._samples)

    def _wait_for_baseline(self) -> bool:
        """Confirm the real application is running before the test rollout.

        Requires baseline_required_successes exact HTTP 200 responses
        through the frontend. A pause container, a crash-looping pod, a
        wrong image, or any stub that is not the real application will
        never produce a 200 here and is rejected before the timing test
        runs — preventing ambiguous 'pod terminated too fast' failures
        that would otherwise be caused by a misconfigured deployment
        rather than a fault.
        """
        deadline = time.monotonic() + self.baseline_timeout_seconds
        while time.monotonic() < deadline:
            samples = self._samples_snapshot()
            successes = sum(1 for _, ok in samples if ok)
            if successes >= self.baseline_required_successes:
                return True
            if len(samples) >= 20 and successes == 0:
                break
            time.sleep(self.poll_interval_seconds)
        print(
            "❌ Deployment does not serve application traffic "
            f"(no HTTP 200 from frontend{self.probe_path})"
        )
        return False
    def _run_active_session_test(self) -> tuple[dict, threading.Thread]:
        import re
        result = {"success": True, "errors": 0}

        def run_wrk():
            cmd = (
                f"kubectl run oracle-load-test-{int(time.time())} -n {self.namespace} "
                f"--image=williamyeh/wrk --rm -i --restart=Never -- "
                f"-t4 -c100 -d25s http://{self.deployment_name}:5000{self.probe_path}"
            )
            out = self.kubectl.exec_command(cmd)
            
            errors = 0
            if "Socket errors" in out:
                match = re.search(r"read (\d+)", out)
                if match:
                    errors += int(match.group(1))
            if "Non-2xx or 3xx responses" in out:
                match = re.search(r"responses:\s*(\d+)", out)
                if match:
                    errors += int(match.group(1))
            
            result["errors"] = errors
            if errors > 0:
                result["success"] = False

        t = threading.Thread(target=run_wrk, daemon=True)
        t.start()
        return result, t
    # ------------------------------------------------------------------
    # Main evaluation
    # ------------------------------------------------------------------

    def evaluate(self) -> dict:
        print("== Ungraceful Termination Mitigation Evaluation ==")

        try:
            deployment = self._get_deployment_json()
            pod_spec = (deployment.get("spec") or {}).get("template", {}).get("spec") or {}
            print(
                f"ℹ️  terminationGracePeriodSeconds="
                f"{pod_spec.get('terminationGracePeriodSeconds', 30)}s (informational)"
            )
        except Exception as e:
            print(f"❌ Could not fetch deployment: {e}")
            return {"success": False}

        self._set_churn_suspended(True)
        probe_started = False
        annotation_applied = False
        original_replicas = (deployment.get("spec") or {}).get("replicas", 1)
        try:
            # --- Check 1: pods stable ---
            print(f"⏳ Waiting up to {self.stabilize_timeout_seconds}s for pods to stabilize...")
            deadline = time.monotonic() + self.stabilize_timeout_seconds
            while time.monotonic() < deadline:
                if self._pods_stable():
                    break
                time.sleep(self.poll_interval_seconds)
            else:
                print("❌ Pods did not stabilize before test rollout")
                return {"success": False}

            # --- Check 2: app-identity baseline ---
            if not self._resolve_frontend_pod():
                return {"success": False}
            print("🚦 Running app-identity baseline check...")
            self._start_probe()
            probe_started = True
            if not self._wait_for_baseline():
                return {"success": False}
            self._stop_probe_thread()
            probe_started = False
            print("✅ Baseline confirmed — real application is serving traffic")

            # --- Check 3: termination duration ---
            # Scale to 1 so the replacement sequence is unambiguous: exactly
            # one pod is terminated, one is started, no load-balancing escape.
            print("⬇️  Scaling to 1 replica for unambiguous termination measurement...")
            self.kubectl.exec_command(
                f"kubectl scale deployment/{self.deployment_name} "
                f"-n {self.namespace} --replicas=1"
            )
            # Wait for scale-down to settle
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                d = self._get_deployment_json()
                if (d.get("status") or {}).get("readyReplicas", 0) == 1:
                    break
                time.sleep(self.poll_interval_seconds)

            old_pod = self._current_pod_name()
            if not old_pod:
                print("❌ Could not identify the current pod before rollout")
                return {"success": False}
            print(f"📌 Tracking pod: {old_pod}")

            initial_generation = (deployment.get("metadata") or {}).get("generation", 0)
            
            print("🌊 Establishing active sessions (load test) before termination...")
            session_result, session_thread = self._run_active_session_test()
            time.sleep(5) 
            
            print("🔄 Triggering test rollout...")
            self._apply_rollout_annotation()
            annotation_applied = True

            probe_deployment = self._get_deployment_json()
            probe_generation = (probe_deployment.get("metadata") or {}).get("generation", 0)
            if probe_generation <= initial_generation:
                print("❌ Test rollout did not increment deployment generation")
                return {"success": False}

            # Measure termination duration of the old pod concurrently
            # with waiting for the rollout to complete.
            duration_result: list[float | None] = [None]

            def measure():
                duration_result[0] = self._measure_termination_duration(old_pod)

            t = threading.Thread(target=measure, daemon=True)
            t.start()

            if not self._wait_for_rollout(probe_generation):
                t.join(timeout=5)
                return {"success": False}

            t.join(timeout=self.disappear_wait_seconds + 10)
            duration = duration_result[0]

            if duration is None:
                print("❌ Could not measure pod termination duration")
                return {"success": False}

            print(
                f"⏱️  Pod termination duration: {duration:.1f}s "
                f"(threshold: {self.min_drain_seconds}s)"
            )

            session_thread.join(timeout=30)
            
            print(
                f"⏱️  Pod termination duration: {duration:.1f}s "
                f"(threshold: {self.min_drain_seconds}s)"
            )

            if not session_result["success"]:
                print(
                    f"❌ Impact Validation Failed: Detected {session_result['errors']} "
                    f"dropped active sessions/errors during the pod handoff."
                )
                return {"success": False}

            if duration < self.min_drain_seconds:
                print(
                    f"❌ Pod terminated in {duration:.1f}s — drain did not complete "
                    f"before SIGKILL (need >= {self.min_drain_seconds}s)"
                )
                return {"success": False}

            print("✅ Zero active sessions dropped during pod handoff.")
            print(
                f"✅ Pod lived {duration:.1f}s during termination — "
                "drain completed before SIGKILL"
            )
            print("✅ Mitigation verified")
            return {"success": True}

        except Exception as e:
            print(f"❌ Error during evaluation: {e}")
            return {"success": False}

        finally:
            if probe_started:
                self._stop_probe_thread()
            # Restore original replica count
            try:
                self.kubectl.exec_command(
                    f"kubectl scale deployment/{self.deployment_name} "
                    f"-n {self.namespace} --replicas={original_replicas}"
                )
            except Exception:
                pass
            if annotation_applied:
                try:
                    self._remove_rollout_annotation()
                except Exception as exc:
                    print(f"⚠️  Could not remove rollout annotation: {exc}")
            self._set_churn_suspended(False)
    