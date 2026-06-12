import base64
import datetime
import logging

from kubernetes import client

from sregym.conductor.oracles.base import Oracle

logger = logging.getLogger(__name__)


class ExpiredTlsMitigationOracle(Oracle):
    """Mitigation oracle for the ExpiredTlsHotelReservation problem.

    The default MitigationOracle only checks pod health, which is insufficient
    here because an expired TLS certificate does not crash any pods. This oracle
    instead verifies that the frontend Ingress no longer references an expired
    TLS certificate, accepting any of the following as a valid mitigation:

    - The faulty Ingress has been deleted entirely.
    - The Ingress still exists but its TLS secret has been replaced with a
    certificate that is currently valid (not_valid_after > now).
    - The Ingress still exists but the TLS secret has been removed entirely.
    """

    def __init__(self, problem):
        super().__init__(problem=problem)

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation (Expired TLS) ==")

        namespace = self.problem.namespace
        networking_v1 = client.NetworkingV1Api()
        v1 = client.CoreV1Api()  # used below to read TLS secrets referenced by the Ingress

        # check if the ingress still exists.
        try:
            ingress = networking_v1.read_namespaced_ingress(name=self.problem.ingress_name, namespace=namespace)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                logger.info("frontend-ingress has been deleted, mitigation accepted.")
                return {"success": True}
            raise

        # check if ingress still exists/check every TLS secret it references.
        if not ingress.spec.tls:
            logger.info("Ingress exists but has no TLS config -- mitigation accepted.")
            return {"success": True}

        now = datetime.datetime.now(datetime.UTC)

        for tls_entry in ingress.spec.tls:
            secret_name = tls_entry.secret_name
            if not secret_name:
                continue

            try:
                secret = v1.read_namespaced_secret(name=secret_name, namespace=namespace)
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    # Secret has been deleted, no expired cert present.
                    continue
                raise

            cert_data = (secret.data or {}).get("tls.crt")
            if not cert_data:
                continue

            expiry = self._get_cert_expiry(cert_data)
            if expiry is None:
                logger.warning("Could not parse certificate in secret '%s'.", secret_name)
                continue

            if expiry <= now:
                logger.info(
                    "Secret '%s' still contains an expired certificate (expired %s).",
                    secret_name,
                    expiry.isoformat(),
                )
                return {
                    "success": False,
                    "reason": (
                        f"Secret '{secret_name}' still contains a certificate that expired on {expiry.isoformat()}."
                    ),
                }

        logger.info("No expired TLS certificates found on the Ingress -- mitigation accepted.")
        return {"success": True}

    @staticmethod
    def _get_cert_expiry(cert_b64: str) -> datetime.datetime | None:
        """Decode a base64-encoded PEM certificate and return its expiry datetime."""
        try:
            from cryptography import x509
        except ImportError:
            logger.error("Missing 'cryptography' library. Run `uv add cryptography`.")
            return None

        try:
            pem_bytes = base64.b64decode(cert_b64)
            cert = x509.load_pem_x509_certificate(pem_bytes)
            return cert.not_valid_after_utc
        except Exception as exc:
            logger.warning("Failed to parse certificate: %s", exc)
            return None
