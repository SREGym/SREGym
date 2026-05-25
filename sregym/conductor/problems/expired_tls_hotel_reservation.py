import os
import subprocess
import logging
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.utils.decorators import mark_fault_injected

# Initialize a standard Python logger
logger = logging.getLogger(__name__)

class ExpiredTlsHotelReservation(Problem):
    def __init__(self):
        self.app = HotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)
        
        self.problem_id = "expired_tls_hotel_reservation"
        self.secret_name = "expired-frontend-cert"
        self.ingress_yaml_path = os.path.join(os.path.dirname(__file__), "manifests", "frontend_ingress.yaml")
        
        self.root_cause = self.build_structured_root_cause(
            component="frontend-ingress",
            namespace=self.namespace,
            description=(
                "The frontend Ingress resource is configured with a TLS secret named "
                f"`{self.secret_name}` which contains an expired TLS certificate. "
                "This breaks HTTPS connections."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

    @mark_fault_injected
    def inject_fault(self) -> bool:
        logger.info("Injecting Expired TLS Certificate fault...")
        self._generate_expired_cert()
        
        # inject the TLS Secret into the cluster
        subprocess.run([
            "kubectl", "create", "secret", "tls", self.secret_name, 
            "--cert=/tmp/tls.crt", "--key=/tmp/tls.key", "-n", self.namespace
        ], check=False)
        
        subprocess.run([
            "kubectl", "apply", "-f", self.ingress_yaml_path, "-n", self.namespace
        ], check=True)
        
        logger.info("Injected expired TLS cert into Ingress.")
        return True

    @mark_fault_injected
    def recover_fault(self) -> bool:
        logger.info("Recovering from Expired TLS Certificate fault...")
        
        subprocess.run(["kubectl", "delete", "-f", self.ingress_yaml_path, "-n", self.namespace], check=False)
        subprocess.run(["kubectl", "delete", "secret", self.secret_name, "-n", self.namespace], check=False)
        
        logger.info("Fault recovered.")
        return True

    def _generate_expired_cert(self):
        logger.info("Generating TLS certificate expired 10 days ago using cryptography...")
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.hazmat.primitives import hashes
            from cryptography.x509.oid import NameOID
            from cryptography import x509
            import datetime
        except ImportError:
            logger.error("Missing 'cryptography' library. Run `uv add cryptography`.")
            raise

        # Generate a private RSA key
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        
        # Setup the Subject/Issuer Name
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, u"hotel.local"),
        ])
        
        # Make the certificate expire 10 days ago
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = x509.CertificateBuilder().subject_name(
            subject
        ).issuer_name(
            issuer
        ).public_key(
            key.public_key()
        ).serial_number(
            x509.random_serial_number()
        ).not_valid_before(
            now - datetime.timedelta(days=10)
        ).not_valid_after(
            now - datetime.timedelta(days=1)
        ).sign(key, hashes.SHA256())

        # Save Private Key to /tmp/
        with open("/tmp/tls.key", "wb") as f:
            f.write(key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption()
            ))

        # Save Public Cert to /tmp/
        with open("/tmp/tls.crt", "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))