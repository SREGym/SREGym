import base64
import contextlib
import datetime
import io
import socket
import ssl
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles import expired_tls_mitigation_oracle as tls_module
from sregym.conductor.oracles.apps.frappe.tls import ExpiredTlsWithProbeOracle
from sregym.conductor.oracles.expired_tls_mitigation_oracle import ExpiredTlsMitigationOracle
from sregym.conductor.oracles.mitigation import MitigationOracle

HOST = "hotel-reservation.local"


def material(host=HOST, *, start=-1, end=30):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + datetime.timedelta(days=start))
        .not_valid_after(now + datetime.timedelta(days=end))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return {
        "tls.crt": base64.b64encode(cert.public_bytes(serialization.Encoding.PEM)).decode(),
        "tls.key": base64.b64encode(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        ).decode(),
    }


@pytest.fixture
def setup(monkeypatch):
    problem = SimpleNamespace(namespace="hotel-reservation", ingress_name="frontend-ingress", HOST=HOST, kubectl=Mock())
    oracle = ExpiredTlsMitigationOracle(problem)
    monkeypatch.setattr(MitigationOracle, "evaluate", Mock(return_value={"success": True}))
    ingress = client.V1Ingress(
        metadata=client.V1ObjectMeta(name="frontend-ingress"),
        spec=client.V1IngressSpec(
            tls=[client.V1IngressTLS(hosts=[HOST], secret_name="renewed-tls")],
            rules=[client.V1IngressRule(host=HOST)],
        ),
    )
    networking, core = Mock(), Mock()
    networking.list_namespaced_ingress.return_value = client.V1IngressList(items=[ingress])
    core.read_namespaced_secret.return_value = client.V1Secret(data=material())
    monkeypatch.setattr(client, "NetworkingV1Api", lambda: networking)
    monkeypatch.setattr(client, "CoreV1Api", lambda: core)
    monkeypatch.setattr(oracle, "_controller_port", lambda: contextlib.nullcontext(12345))
    monkeypatch.setattr(oracle, "_verify_served_certificate", Mock())
    return oracle, ingress, core, networking


@pytest.mark.parametrize(
    "change", ["expired", "future", "invalid_pem", "invalid_base64", "empty", "missing_key", "wrong_key"]
)
def test_invalid_secret_fails_before_handshake(setup, change):
    oracle, _, core, _ = setup
    data = material()
    if change == "expired":
        data = material(start=-10, end=-1)
    elif change == "future":
        data = material(start=1)
    elif change == "invalid_pem":
        data["tls.crt"] = base64.b64encode(b"not a certificate").decode()
    elif change == "invalid_base64":
        data["tls.crt"] = "!"
    elif change == "empty":
        data["tls.crt"] = ""
    elif change == "missing_key":
        del data["tls.key"]
    elif change == "wrong_key":
        data["tls.key"] = material()["tls.key"]
    core.read_namespaced_secret.return_value.data = data
    assert oracle.evaluate()["reason"] == "fault_still_present"
    oracle._verify_served_certificate.assert_not_called()


def test_renewed_secret_name_is_not_fixed(setup):
    oracle, _, core, _ = setup
    assert oracle.evaluate() == {"success": True}
    core.read_namespaced_secret.assert_called_once_with("renewed-tls", "hotel-reservation")
    assert oracle._verify_served_certificate.call_args.args[:2] == (12345, HOST)


def test_rule_host_is_used_when_tls_hosts_are_omitted(setup):
    oracle, ingress, _, _ = setup
    ingress.spec.tls[0].hosts = None
    assert oracle.evaluate()["success"] is True
    assert oracle._verify_served_certificate.call_args.args[1] == HOST


@pytest.mark.parametrize("case", ["no_hostname", "missing_secret"])
def test_incomplete_tls_route_fails(setup, case):
    oracle, ingress, core, _ = setup
    if case == "no_hostname":
        ingress.spec.tls[0].hosts = []
        ingress.spec.rules = []
    else:
        core.read_namespaced_secret.side_effect = ApiException(status=404)
    assert oracle.evaluate()["reason"] == "fault_still_present"


@pytest.mark.parametrize("case", ["deleted_ingress", "removed_tls"])
def test_return_to_http_baseline_is_valid(setup, case):
    oracle, ingress, _, networking = setup
    if case == "deleted_ingress":
        networking.list_namespaced_ingress.return_value.items = []
    else:
        ingress.spec.tls = []
    assert oracle.evaluate() == {"success": True}
    oracle._verify_served_certificate.assert_not_called()


@pytest.mark.parametrize("api", ["ingress", "secret"])
def test_api_unavailability_is_not_a_success(setup, api):
    oracle, _, core, networking = setup
    reader = networking.list_namespaced_ingress if api == "ingress" else core.read_namespaced_secret
    reader.side_effect = ApiException(status=503)
    with pytest.raises(ApiException):
        oracle.evaluate()


def test_unhealthy_application_short_circuits_tls(setup, monkeypatch):
    oracle, _, _, networking = setup
    failure = oracle.fail("required_deployment_scaled_to_zero")
    monkeypatch.setattr(MitigationOracle, "evaluate", lambda self: failure)
    assert oracle.evaluate() == failure
    networking.list_namespaced_ingress.assert_not_called()


def test_renaming_ingress_does_not_hide_bad_certificate(setup):
    oracle, ingress, core, _ = setup
    ingress.metadata.name = "replacement-ingress"
    core.read_namespaced_secret.return_value.data = material(start=1)
    assert oracle.evaluate()["reason"] == "fault_still_present"


def test_valid_renamed_ingress_passes(setup):
    oracle, ingress, _, _ = setup
    ingress.metadata.name = "replacement-ingress"
    assert oracle.evaluate()["success"] is True


def test_unrelated_ingress_is_not_part_of_this_fault(setup):
    oracle, _, core, networking = setup
    networking.list_namespaced_ingress.return_value.items.append(
        client.V1Ingress(
            metadata=client.V1ObjectMeta(name="unrelated-ingress"),
            spec=client.V1IngressSpec(
                rules=[client.V1IngressRule(host="other.example")],
                tls=[client.V1IngressTLS(hosts=["other.example"], secret_name="unrelated-tls")],
            ),
        )
    )
    assert oracle.evaluate()["success"] is True
    core.read_namespaced_secret.assert_called_once_with("renewed-tls", "hotel-reservation")


def test_controller_default_certificate_is_verified(setup):
    oracle, ingress, core, _ = setup
    ingress.spec.tls[0].secret_name = None
    assert oracle.evaluate()["success"] is True
    core.read_namespaced_secret.assert_not_called()
    context = oracle._verify_served_certificate.call_args.args[2]
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize("future", [False, True])
def test_frappe_wrapper_keeps_its_application_probe(setup, monkeypatch, future):
    original, ingress, core, networking = setup
    original.problem.HOST = "erp.frappe.local"
    ingress.spec.rules[0].host = original.problem.HOST
    ingress.spec.tls[0].hosts = [original.problem.HOST]
    core.read_namespaced_secret.return_value.data = material(original.problem.HOST, start=1 if future else -1)
    networking.read_namespaced_ingress.return_value = ingress
    oracle = ExpiredTlsWithProbeOracle(original.problem)
    monkeypatch.setattr(oracle, "_controller_port", lambda: contextlib.nullcontext(12345))
    monkeypatch.setattr(oracle, "_verify_served_certificate", Mock())
    probe = Mock(return_value=("200", "valid certificate", False))
    monkeypatch.setattr(oracle, "https_probe", probe)
    assert oracle.evaluate()["success"] is (not future)
    assert probe.call_count == (0 if future else 1)


@pytest.mark.parametrize(
    "error,reason",
    [
        (ssl.SSLCertVerificationError("expired"), "fault_still_present"),
        (ConnectionRefusedError(), "connectivity_probe_failed"),
    ],
)
def test_served_tls_failure_is_not_ignored(setup, error, reason):
    oracle, _, _, _ = setup
    oracle._verify_served_certificate.side_effect = error
    assert oracle.evaluate()["reason"] == reason


@pytest.mark.parametrize("started", [True, False])
def test_port_forward_is_owned_and_cleaned_up(monkeypatch, started):
    process = Mock()
    process.args = ["kubectl", "port-forward"]
    process.stdout = io.StringIO("Forwarding from 127.0.0.1:32123 -> 443\n" if started else "")
    process.poll.return_value = None if started else 1
    selector = Mock()
    selector.__enter__ = Mock(return_value=selector)
    selector.__exit__ = Mock(return_value=False)
    selector.select.return_value = [True]
    monkeypatch.setattr(tls_module.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(tls_module.selectors, "DefaultSelector", lambda: selector)
    oracle = ExpiredTlsMitigationOracle(SimpleNamespace())
    if started:
        with oracle._controller_port() as port:
            assert port == 32123
    else:
        with pytest.raises(subprocess.CalledProcessError), oracle._controller_port():
            pytest.fail("Failed port-forward must not yield a port")
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=5)
    assert process.stdout.closed


@pytest.mark.parametrize("served_host,expected", [(HOST, True), ("wrong.example", False)])
@pytest.mark.parametrize("strict", [False, True])
def test_real_tls_handshake_checks_hostname(tmp_path, served_host, expected, strict):
    data = material(served_host)
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert.write_bytes(base64.b64decode(data["tls.crt"]))
    key.write_bytes(base64.b64decode(data["tls.key"]))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    context = ExpiredTlsMitigationOracle._tls_context(data)
    if strict:
        context.verify_flags |= ssl.VERIFY_X509_STRICT
    oracle = ExpiredTlsMitigationOracle(SimpleNamespace())
    oracle.TLS_SETTLE_SECONDS = 0
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)

        def serve():
            with (
                listener.accept()[0] as connection,
                contextlib.suppress(ssl.SSLError),
                server_context.wrap_socket(connection, server_side=True),
            ):
                pass

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        try:
            if expected:
                oracle._verify_served_certificate(listener.getsockname()[1], HOST, context)
            else:
                with pytest.raises(ssl.SSLCertVerificationError):
                    oracle._verify_served_certificate(listener.getsockname()[1], HOST, context)
        finally:
            worker.join(timeout=5)
        assert not worker.is_alive()
