import base64
import binascii
import contextlib
import datetime
import logging
import re
import selectors
import socket
import ssl
import subprocess
import time

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from kubernetes import client

from sregym.conductor.oracles.mitigation import MitigationOracle

logger = logging.getLogger(__name__)


class ExpiredTlsMitigationOracle(MitigationOracle):
    """Check application health and the TLS configuration actually served.

    The fault adds an Ingress outside the healthy baseline, so deleting it or
    removing TLS remains valid. A retained TLS route must have usable key
    material and complete a verified handshake for its configured hostname.
    """

    TLS_SETTLE_SECONDS = 30
    CONTROLLER_SERVICE = "svc/ingress-nginx-controller"
    CONTROLLER_NAMESPACE = "ingress-nginx"

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation (Expired TLS) ==")

        result = super().evaluate()
        if not result.get("success"):
            return result

        ingresses = client.NetworkingV1Api().list_namespaced_ingress(self.problem.namespace).items
        routes = [
            ingress
            for ingress in ingresses
            if ingress.metadata.name == self.problem.ingress_name
            or any(rule.host == self.problem.HOST for rule in ingress.spec.rules or [])
            or any(self.problem.HOST in (entry.hosts or []) for entry in ingress.spec.tls or [])
        ]
        if not any(ingress.spec.tls for ingress in routes):
            logger.info("Fault-added TLS configuration has been removed.")
            return result

        contexts = []
        for ingress in routes:
            for entry in ingress.spec.tls or []:
                hosts = entry.hosts or [rule.host for rule in ingress.spec.rules or [] if rule.host]
                if not hosts:
                    return self.fail("fault_still_present", error="TLS route has no hostname")
                if not entry.secret_name:
                    contexts.extend((host, ssl.create_default_context()) for host in hosts)
                    continue
                try:
                    secret = client.CoreV1Api().read_namespaced_secret(entry.secret_name, self.problem.namespace)
                except client.exceptions.ApiException as exc:
                    if exc.status != 404:
                        raise
                    return self.fail("fault_still_present", secret=entry.secret_name, error="TLS Secret is missing")
                try:
                    context = self._tls_context(secret.data or {})
                except (ValueError, TypeError, binascii.Error, UnsupportedAlgorithm, ssl.SSLError) as exc:
                    logger.info("Invalid TLS material in Secret '%s': %s", entry.secret_name, exc)
                    return self.fail("fault_still_present", secret=entry.secret_name, error=str(exc))
                contexts.extend((host, context) for host in hosts)

        try:
            with self._controller_port() as port:
                for host, context in contexts:
                    self._verify_served_certificate(port, host, context)
        except ssl.SSLCertVerificationError as exc:
            return self.fail("fault_still_present", error=str(exc))
        except (OSError, ssl.SSLError) as exc:
            return self.fail("connectivity_probe_failed", error=str(exc))

        logger.info("Configured TLS routes complete verified handshakes.")
        return result

    @staticmethod
    def _tls_context(data: dict[str, str]) -> ssl.SSLContext:
        """Validate the leaf/key and trust the configured certificate chain.

        The benchmark uses a private, self-signed certificate. Trusting its
        public material still verifies dates, hostname, and the served key;
        requiring a public CA would reject the intended renewal.
        """
        try:
            pem = base64.b64decode(data["tls.crt"], validate=True)
            key_pem = base64.b64decode(data["tls.key"], validate=True)
        except KeyError as exc:
            raise ValueError(f"TLS Secret is missing {exc.args[0]}") from exc
        certificate = x509.load_pem_x509_certificates(pem)[0]
        now = datetime.datetime.now(datetime.UTC)
        if not certificate.not_valid_before_utc <= now < certificate.not_valid_after_utc:
            raise ValueError(
                f"Certificate is not currently valid ({certificate.not_valid_before_utc.isoformat()} "
                f"to {certificate.not_valid_after_utc.isoformat()})"
            )
        key = serialization.load_pem_private_key(key_pem, password=None)
        encoding = serialization.Encoding.DER
        key_format = serialization.PublicFormat.SubjectPublicKeyInfo
        if certificate.public_key().public_bytes(encoding, key_format) != key.public_key().public_bytes(
            encoding, key_format
        ):
            raise ValueError("Certificate and private key do not match")
        context = ssl.create_default_context()
        context.load_verify_locations(cadata=pem.decode("ascii"))
        context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        return context

    def _verify_served_certificate(self, port: int, host: str, context: ssl.SSLContext) -> None:
        # Allow the controller to observe a Secret update, but use a fresh
        # connection each time so a cached expired certificate cannot pass.
        deadline = time.monotonic() + self.TLS_SETTLE_SECONDS
        while True:
            try:
                with (
                    socket.create_connection(("127.0.0.1", port), timeout=5) as connection,
                    context.wrap_socket(connection, server_hostname=host),
                ):
                    return
            except (OSError, ssl.SSLError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(2)

    @contextlib.contextmanager
    def _controller_port(self):
        """Reach the controller through the API, on KIND and external clusters."""
        process = subprocess.Popen(
            [
                "kubectl",
                "port-forward",
                "--address=127.0.0.1",
                "-n",
                self.CONTROLLER_NAMESPACE,
                self.CONTROLLER_SERVICE,
                ":443",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if not selector.select(timeout=max(0, deadline - time.monotonic())):
                        break
                    line = process.stdout.readline()
                    match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+) ->", line)
                    if match:
                        yield int(match.group(1))
                        return
                    if not line and process.poll() is not None:
                        break
            raise subprocess.CalledProcessError(
                process.poll() or 1, process.args, output="Could not start the ingress controller port-forward"
            )
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
