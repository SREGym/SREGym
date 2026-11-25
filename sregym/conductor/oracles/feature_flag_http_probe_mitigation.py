"""Verify that the frontend Service serves hotel searches after mitigation."""

import json
import time

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.http_probe import find_probe_pod, wget_from_pod

_HOTELS_URL = "http://frontend:5000/hotels?inDate=2015-04-09&outDate=2015-04-10&lat=37.7749&lon=-122.4194"


class FeatureFlagHttpProbeMitigationOracle(Oracle):
    """Verify the frontend Service returns real hotel search results.

    Probes via kubectl exec into an existing non-frontend pod in the
    namespace — no ephemeral pod needed, no Prometheus dependency.
    """

    importance = 1.0

    def __init__(self, problem, probe_attempts: int = 5):
        super().__init__(problem)
        self.probe_attempts = probe_attempts

    @staticmethod
    def _valid_hotel_response(output: str) -> bool:
        """Reject 200 responses that are not non-empty hotel search results."""
        start = output.find("{")
        if start < 0:
            return False
        try:
            payload, _ = json.JSONDecoder().raw_decode(output[start:])
        except json.JSONDecodeError:
            return False
        if not isinstance(payload, dict):
            return False
        features = payload.get("features")
        return (
            payload.get("type") == "FeatureCollection"
            and isinstance(features, list)
            and bool(features)
            and all(
                isinstance(feature, dict)
                and isinstance(feature.get("id"), str)
                and bool(feature["id"])
                and isinstance(feature.get("properties"), dict)
                and isinstance(feature["properties"].get("name"), str)
                and bool(feature["properties"]["name"])
                for feature in features
            )
        )

    def _get_probe_pod(self) -> str | None:
        """Find the consul pod as a reliable probe origin — always present
        in hotel-reservation and guaranteed to have wget."""
        return find_probe_pod(self.problem.kubectl, self.problem.namespace)

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
            statuses, result = wget_from_pod(kubectl, probe_pod, namespace, _HOTELS_URL)
            if not statuses:
                return self.fail("http_probe_response_missing", pod=probe_pod, output=result.strip()[:500])
            if statuses[-1] == "200" and self._valid_hotel_response(result):
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
