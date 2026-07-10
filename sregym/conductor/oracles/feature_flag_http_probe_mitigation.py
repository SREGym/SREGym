"""HTTP probe mitigation oracle for feature flag latent bug problem."""

import re
import time

from sregym.conductor.oracles.base import Oracle


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

    def evaluate(self) -> dict:
        print("== HTTP Probe Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        results = {}

        probe_pod = self._get_probe_pod()
        if not probe_pod:
            print("❌ No suitable probe pod found")
            results["success"] = False
            return results

        print(f"Probing frontend via pod {probe_pod}...")

        success_count = 0
        for _i in range(self.probe_attempts):
            cmd = (
                f"kubectl exec {probe_pod} -n {namespace} -- "
                f"wget -S -q -O /dev/null "
                f"'http://frontend:5000/hotels?inDate=2015-04-09&outDate=2015-04-10&lat=37.7749&lon=-122.4194'"
                f" 2>&1 || true"
            )
            result = kubectl.exec_command(cmd)
            if re.search(r"HTTP/\S+ 200", str(result)):
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
            results["success"] = False

        return results
