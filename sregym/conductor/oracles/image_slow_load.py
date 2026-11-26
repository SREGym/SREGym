import time

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.generators.workload.image_client import ImageEvidenceError, wait_for_images


class ImageSlowLoadOracle(Oracle):
    """Grade fresh product images, not whether an alert happens to be silent."""

    evaluation_timeout_seconds = 135
    FAILURE_CLASSES = {
        "image_evidence_missing": FailureClass.ENVIRONMENT_ERROR,
        "image_measurement_unavailable": FailureClass.ENVIRONMENT_ERROR,
        "image_request_failed": FailureClass.AMBIGUOUS,
        "image_latency_outside_target": FailureClass.AMBIGUOUS,
    }

    def evaluate(self, solution=None, trace=None, duration=None):
        try:
            evidence = wait_for_images(self.problem.kubectl, after=time.time(), timeout=120)
        except ImageEvidenceError as exc:
            print(f"[FAIL] {exc.reason}: {exc}", flush=True)
            return self.fail(exc.reason, evidence=str(exc))
        print(f"[PASS] Product images load within the healthy latency budget: {evidence}", flush=True)
        return {"success": True}
