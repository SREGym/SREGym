"""HTTP probe mitigation oracle for feature flag latent bug problem."""

import re
import shlex
import time

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass


class FeatureFlagHttpProbeMitigationOracle(Oracle):
    """Verifies the frontend /hotels endpoint returns HTTP 200.

    Probes via kubectl exec into an existing non-frontend pod in the
    namespace — no ephemeral pod needed, no Prometheus dependency.
    """

    importance = 1.0

    def __init__(self, problem, probe_attempts: int = 5):
        super().__init__(problem)
        self.probe_attempts = probe_attempts

    def _get_probe_pod(self) -> str | None:
        """Find the consul pod as a reliable probe origin — always present
        in hotel-reservation and guaranteed to have wget."""
        pod_list = self.problem.kubectl.list_pods(self.problem.namespace)
        for pod in pod_list.items:
            if pod.status.phase == "Running" and pod.metadata.name and pod.metadata.name.startswith("consul"):
                return pod.metadata.name
        # Fallback: any running non-frontend, non-wrk2 pod
        for pod in pod_list.items:
            if (
                pod.status.phase == "Running"
                and pod.metadata.name
                and "frontend" not in pod.metadata.name
                and "wrk2" not in pod.metadata.name
            ):
                return pod.metadata.name
        return None

    FAILURE_CLASSES = {
        # Measured over several attempts against the endpoint the feature flag
        # breaks, so a sustained error rate is the fault's symptom rather than
        # one unlucky request.
        "endpoint_error_rate_high": FailureClass.AGENT_ERROR,
        "http_probe_response_missing": FailureClass.AMBIGUOUS,
        # No probe pod means no measurement, which is our problem.
        "no_probe_pod_available": FailureClass.HARNESS_ERROR,
    }

    def evaluate(self) -> dict:
        try:
            return self._evaluate()
        except Exception as exc:
            print(f"[FAIL] Error running frontend HTTP probe: {exc}")
            return self.fail_from_exception(exc)

    def _evaluate(self) -> dict:
        print("== HTTP Probe Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        results = {}

        probe_pod = self._get_probe_pod()
        if not probe_pod:
            print("❌ No suitable probe pod found")
            # We had nowhere to run the probe from, so nothing was measured.
            return self.fail("no_probe_pod_available", namespace=namespace)

        print(f"Probing frontend via pod {probe_pod}...")

        success_count = 0
        for _i in range(self.probe_attempts):
            # wget exits nonzero for HTTP errors. Preserve its response inside the
            # pod, without masking a failure to execute the command through Kubernetes.
            script = (
                "wget -T 10 -S -q -O /dev/null "
                "'http://frontend:5000/hotels?inDate=2015-04-09&outDate=2015-04-10&lat=37.7749&lon=-122.4194'"
                " 2>&1 || true"
            )
            cmd = f"kubectl exec {shlex.quote(probe_pod)} -n {shlex.quote(namespace)} -- sh -c {shlex.quote(script)}"
            result = kubectl.exec_command_checked(cmd, timeout=30)
            statuses = re.findall(r"^\s*HTTP/\S+\s+(\d{3})\b", result, re.MULTILINE)
            if not statuses:
                return self.fail("http_probe_response_missing", pod=probe_pod, output=result.strip()[:500])
            if statuses[-1] == "200":
                success_count += 1
            time.sleep(0.5)

        success_rate = success_count / self.probe_attempts
        print(f"HTTP probe success rate: {success_count}/{self.probe_attempts}")

        if success_rate >= 0.8:
            print("✅ Frontend /hotels endpoint returning 200 OK")
            results["success"] = True
        else:
            print(
                f"❌ Frontend /hotels endpoint returning errors ({self.probe_attempts - success_count}/{self.probe_attempts} failed)"
            )
            results.update(
                self.fail(
                    "endpoint_error_rate_high",
                    endpoint="/hotels",
                    succeeded=success_count,
                    attempts=self.probe_attempts,
                )
            )

        return results
