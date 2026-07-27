import contextlib
import logging
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle

logger = logging.getLogger(__name__)


class PostgresLockMitigationOracle(Oracle):
    """
    Mitigation oracle for the postgres lock-contention problem:

    The default MitigationOracle only checks the pod health, which is insufficient.

    Here, the fault holds a database lock, so every pod stays Running the whole
    time.

    It delegates to the problem's `_catalog_read_status()` helper, which runs a
    short-lock_timeout read of catalog.products and reports:
     - "ok" (readable),
     - "locked" (blocked by an ACCESS EXCLUSIVE lock)
     - "other" (some other failure)

    Mitigation is accepted only if the table stays readable for a short window,
    not just on a single read. A momentary success (e.g. right after a backend is
    terminated but before a holder could re-acquire the lock) must not be mistaken
    for a real fix, so we require readability to persist.
    """

    # Require catalog.products to stay readable for this long before accepting.
    SUSTAINED_SECONDS = 8
    SAMPLE_INTERVAL = 2
    PROBE_TIMEOUT_SECONDS = 60
    PROBE_INTERVAL_SECONDS = 2
    REQUEST_TIMEOUT_SECONDS = 5
    PRODUCT_PATH = "/api/products"
    EXPECTED_PRODUCT_ID = "OLJCESPC7Z"

    def __init__(self, problem):
        super().__init__(problem=problem)

    def _product_catalog_available(self) -> bool:
        """Verify the product catalog through the application's public API."""
        namespace = self.problem.namespace
        service_name = self.problem.app.frontend_service
        core_v1 = self.problem.kubectl.core_v1_api
        pod_name = f"catalog-mitigation-check-{time.time_ns()}"[:63]

        try:
            service = core_v1.read_namespaced_service(name=service_name, namespace=namespace)
            service_ports = service.spec.ports or []
            if not service_ports:
                logger.info("Frontend service '%s' has no ports.", service_name)
                return False

            url = f"http://{service_name}.{namespace}.svc.cluster.local:{service_ports[0].port}{self.PRODUCT_PATH}"
            script = (
                "set -eu; "
                f"wget -q -T {self.REQUEST_TIMEOUT_SECONDS} -t 1 -O /tmp/products '{url}'; "
                f"grep -q '{self.EXPECTED_PRODUCT_ID}' /tmp/products; "
                "echo PRODUCTS_OK"
            )
            pod = client.V1Pod(
                metadata=client.V1ObjectMeta(
                    name=pod_name,
                    namespace=namespace,
                    labels={"app": "catalog-mitigation-check"},
                ),
                spec=client.V1PodSpec(
                    restart_policy="Never",
                    automount_service_account_token=False,
                    containers=[
                        client.V1Container(
                            name="probe",
                            image="busybox:1.36",
                            image_pull_policy="IfNotPresent",
                            command=["sh", "-c", script],
                        )
                    ],
                ),
            )
            core_v1.create_namespaced_pod(namespace=namespace, body=pod)

            deadline = time.monotonic() + self.PROBE_TIMEOUT_SECONDS
            phase = "Pending"
            while time.monotonic() < deadline:
                current = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
                phase = current.status.phase or "Pending"
                if phase in ("Succeeded", "Failed"):
                    break
                time.sleep(self.PROBE_INTERVAL_SECONDS)

            logs = core_v1.read_namespaced_pod_log(name=pod_name, namespace=namespace)
            return phase == "Succeeded" and "PRODUCTS_OK" in logs
        except ApiException as exc:
            logger.info("Product catalog API probe failed: %s", exc)
            return False
        finally:
            with contextlib.suppress(ApiException):
                core_v1.delete_namespaced_pod(
                    name=pod_name,
                    namespace=namespace,
                    grace_period_seconds=0,
                )

    def evaluate(self) -> dict:
        print("--- Mitigation Evaluation (Postgres lock contention) ---")

        deadline = time.monotonic() + self.SUSTAINED_SECONDS
        while True:
            status = self.problem._catalog_read_status()
            if status != "ok":
                logger.info("catalog.products read status is '%s'; not mitigated.", status)
                return {
                    "success": False,
                    "reason": (
                        "A read of catalog.products did not succeed (status="
                        f"{status}); an ACCESS EXCLUSIVE lock is still held on the table."
                    ),
                }
            if time.monotonic() >= deadline:
                break
            time.sleep(self.SAMPLE_INTERVAL)

        logger.info(
            "catalog.products stayed readable for %ss; lock condition cleared.",
            self.SUSTAINED_SECONDS,
        )

        if not self._product_catalog_available():
            return {
                "success": False,
                "reason": "The product catalog is not reachable through the frontend API with its expected data.",
            }

        logger.info("Product catalog API returned the expected catalog item; mitigation accepted.")
        return {"success": True}
