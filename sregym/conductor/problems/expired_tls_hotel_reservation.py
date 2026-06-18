import base64
import logging
import os

from kubernetes import client

from sregym.conductor.oracles.expired_tls_mitigation_oracle import ExpiredTlsMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.observer.ingress_nginx import IngressNginx
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class ExpiredTlsHotelReservation(Problem):
    """
    Fault: the frontend Ingress references a TLS secret containing an expired certificate.

    All pods remain Running and healthy, the failure is invisible at the pod level.
    An agent must inspect the Ingress TLS configuration and decode the certificate
    inside the referenced secret to discover the root cause.
    """

    def __init__(self):
        super().__init__(app=HotelReservation())

        self.kubectl = KubeCtl()
        self.problem_id = "expired_tls_hotel_reservation"
        self.secret_name = "hotel-frontend-tls"
        self.ingress_name = "frontend-ingress"
        self.faulty_service = ["frontend"]
        self.ingress_yaml_path = os.path.join(os.path.dirname(__file__), "manifests", "frontend_ingress.yaml")

        self.root_cause = self.build_structured_root_cause(
            component=self.ingress_name,
            namespace=self.namespace,
            description=(
                # "The frontend Ingress is configured with a TLS secret named "
                # f"`{self.secret_name}` which contains an expired TLS certificate. "
                # "The NGINX Ingress controller detects the expired certificate, logs a "
                # "validation error, and falls back to its default certificate. "
                # "Clients connecting to hotel-reservation.local receive an unexpected "
                # "certificate rather than the configured one, and the controller logs "
                # "show the expiry error."
                "The frontend Ingress resource is configured with a TLS secret named "
                f"`{self.secret_name}` which contains an expired TLS certificate. "
                "This breaks HTTPS connections."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = ExpiredTlsMitigationOracle(problem=self)
        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self) -> bool:
        logger.info("Injecting Expired TLS Certificate fault...")

        IngressNginx().deploy()

        cert_pem, key_pem = self._generate_expired_cert()
        self._create_tls_secret(cert_pem, key_pem)

        KubeCtl().apply_configs(self.namespace, self.ingress_yaml_path)

        # Delete the NGINX default certificate so it cannot fall back to a working cert.
        # This forces clients to receive the expired certificate directly.
        # Uncomment to enable strict TLS failure.
        # v1 = client.CoreV1Api()
        # try:
        #     v1.delete_namespaced_secret(
        #         name="ingress-nginx-admission",
        #         namespace="ingress-nginx"
        #     )
        # except client.exceptions.ApiException:
        #     pass

        logger.info("Injected expired TLS cert into Ingress.")
        return True

    @mark_fault_injected
    def recover_fault(self) -> bool:
        logger.info("Recovering from Expired TLS Certificate fault...")

        # If the default cert deletion above is enabled, restore it here by
        # redeploying IngressNginx or recreating the secret.
        # IngressNginx().deploy()

        KubeCtl().delete_configs(self.namespace, self.ingress_yaml_path)

        v1 = client.CoreV1Api()
        try:
            v1.delete_namespaced_secret(name=self.secret_name, namespace=self.namespace)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise

        logger.info("Fault recovered.")
        return True

    def _create_tls_secret(self, cert_pem: bytes, key_pem: bytes) -> None:
        """Create (or replace) the Kubernetes TLS secret from raw PEM bytes."""
        v1 = client.CoreV1Api()
        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(name=self.secret_name, namespace=self.namespace),
            type="kubernetes.io/tls",
            data={
                "tls.crt": base64.b64encode(cert_pem).decode(),
                "tls.key": base64.b64encode(key_pem).decode(),
            },
        )
        try:
            v1.create_namespaced_secret(namespace=self.namespace, body=secret)
        except client.exceptions.ApiException as e:
            if e.status == 409:
                # Secret already exists - replace it so the cert is fresh each run.
                v1.replace_namespaced_secret(name=self.secret_name, namespace=self.namespace, body=secret)
            else:
                raise

    def _generate_expired_cert(self) -> tuple[bytes, bytes]:
        """Generate a self-signed TLS certificate that expired one day ago.

        Returns:
            A (cert_pem, key_pem) tuple of raw PEM-encoded bytes.
        """
        try:
            import datetime

            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.x509.oid import NameOID
        except ImportError as exc:
            raise ImportError("Missing 'cryptography' library. Run `uv add cryptography`.") from exc

        logger.info("Generating TLS certificate expired 1 day ago...")

        # 65537 (0x10001) is the standard RSA public exponent
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        subject = issuer = x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, "hotel-reservation.local"),
            ]
        )
        now = datetime.datetime.now(datetime.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            # Certificate was "valid" for 9 days but expired yesterday
            # the -10 day start avoids clocks-skew rejection on strict validators.
            .not_valid_before(now - datetime.timedelta(days=10))
            .not_valid_after(now - datetime.timedelta(days=1))  # expired yesterday
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName("hotel-reservation.local"),
                    ]
                ),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )

        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        key_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )

        return cert_pem, key_pem
