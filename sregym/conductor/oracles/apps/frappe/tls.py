"""Frappe: tls."""

from __future__ import annotations

import datetime

from kubernetes import client

from sregym.conductor.oracles.expired_tls_mitigation_oracle import ExpiredTlsMitigationOracle


class ExpiredTlsWithProbeOracle(ExpiredTlsMitigationOracle):
    """The original state check, then (if the Ingress still has TLS) an HTTPS probe through the controller:
    the certificate it serves for the host must not be expired and the site must answer."""

    CONTROLLER = "ingress-nginx-controller.ingress-nginx.svc.cluster.local"

    def evaluate(self) -> dict:
        result = super().evaluate()
        if not result.get("success"):
            return result
        problem = self.problem
        try:
            ingress = client.NetworkingV1Api().read_namespaced_ingress(problem.ingress_name, problem.namespace)
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return result
            raise
        if not ingress.spec.tls:
            return result
        try:
            status, not_after, expired = self.https_probe()
        except Exception as exc:
            print(f"❌ HTTPS probe through the ingress failed: {str(exc)[-300:]}")
            return self.fail("fault_still_present", probe_error=str(exc)[-300:])
        if expired or status != "200":
            print(f"❌ HTTPS through the ingress: status={status} notAfter={not_after} expired={expired}")
            return self.fail("fault_still_present", status=status, not_after=not_after)
        print(f"✅ HTTPS through the ingress: status={status}, certificate valid until {not_after}")
        return result

    def https_probe(self) -> tuple[str, str, bool]:
        """GET /api/method/ping over HTTPS via the controller (from the toolbox); returns
        (HTTP status, certificate notAfter, whether that certificate has expired)."""
        host = self.problem.HOST
        script = (
            "import socket, ssl, subprocess\n"
            f"host, addr = {host!r}, {self.CONTROLLER!r}\n"
            "ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE\n"
            "s = ctx.wrap_socket(socket.create_connection((addr, 443), timeout=10), server_hostname=host)\n"
            "der = s.getpeercert(binary_form=True)\n"
            "s.sendall(f'GET /api/method/ping HTTP/1.1\\r\\nHost: {host}\\r\\nConnection: close\\r\\n\\r\\n'.encode())\n"
            "status = s.recv(64).split(b' ')[1].decode()\n"
            "pem = ssl.DER_cert_to_PEM_cert(der)\n"
            "end = subprocess.run(['openssl', 'x509', '-noout', '-enddate'], input=pem, capture_output=True, text=True)\n"
            "print('STATUS', status, end.stdout.strip())\n"
        )
        out = self.problem.app.toolbox_exec("python3 -", input_data=script, timeout=60)
        parts = out.split()
        status = parts[1] if len(parts) > 1 else ""
        not_after = out.split("notAfter=", 1)[1].strip() if "notAfter=" in out else ""
        expired = True
        if not_after:
            expiry = datetime.datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=datetime.UTC)
            expired = expiry <= datetime.datetime.now(datetime.UTC)
        return status, not_after, expired
