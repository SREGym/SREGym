"""Frappe: networking."""

from __future__ import annotations

import datetime
import json
import shlex
import textwrap

from kubernetes import client

from sregym.conductor.oracles.apps.frappe.state import FrappeFaultStateOracle
from sregym.conductor.oracles.apps.frappe.tls import ExpiredTlsWithProbeOracle
from sregym.conductor.oracles.conntrack_mitigation import ConntrackMitigationOracle
from sregym.conductor.problems.apps.frappe.helpers import (
    APP,
    GUNICORN,
    WEB_SERVICE,
    apply_manifest,
    replace_pods_with_patch,
    rollout_status,
    selector_of,
)
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.environment import configure_port, make_app
from sregym.conductor.problems.ephemeral_port_range_hotel_reservation import BAD_RANGE as EPHEMERAL_BAD_RANGE
from sregym.conductor.problems.ephemeral_port_range_hotel_reservation import SYSCTL_NAME as EPHEMERAL_SYSCTL
from sregym.conductor.problems.expired_tls_hotel_reservation import ExpiredTlsHotelReservation
from sregym.conductor.problems.node_conntrack_exhaustion import NodeConntrackExhaustionHotelReservation
from sregym.observer.ingress_nginx import IngressNginx
from sregym.utils.decorators import mark_fault_injected


class EphemeralPortRangeFrappe(Problem):
    """``net.ipv4.ip_local_port_range`` narrowed to four ports on gunicorn.

    Frappe opens a fresh MariaDB connection for every request (and closes it,
    leaving the client side in TIME_WAIT), so four ephemeral ports run out at
    once and outbound connects fail with EADDRNOTAVAIL while the pod runs.
    """

    SYSCTL = EPHEMERAL_SYSCTL
    BAD_RANGE = EPHEMERAL_BAD_RANGE
    MIN_PORTS = 1024

    def __init__(self, app_name: str = APP, faulty_service: str = GUNICORN):
        super().__init__(app=make_app(app_name))
        self.faulty_service = faulty_service
        configure_port(
            self,
            app_name,
            component=f"deployment/{faulty_service}",
            description=(
                f"The `{faulty_service}` pod template sets the namespaced sysctl `{self.SYSCTL}` to "
                f"'{self.BAD_RANGE}' in its securityContext, narrowing the ephemeral port range to 4 ports. "
                "Gunicorn opens a new TCP connection to MariaDB (and Redis) for its requests; the few local ports are "
                "quickly exhausted (closed connections sit in TIME_WAIT), so outbound connects fail with "
                "EADDRNOTAVAIL ('Cannot assign requested address') and the Desk/API requests behind "
                f"`{WEB_SERVICE}` fail, although the pod stays Running. Fix: remove the sysctl (or restore a normal range)."
            ),
            oracle_factory=FrappeFaultStateOracle,
        )

    @staticmethod
    def _sysctl_patch(value):
        sysctls = None if value is None else [{"name": EPHEMERAL_SYSCTL, "value": value}]
        return {"spec": {"template": {"spec": {"securityContext": {"sysctls": sysctls}}}}}

    @mark_fault_injected
    def inject_fault(self):
        replace_pods_with_patch(self, self.faulty_service, self._sysctl_patch(self.BAD_RANGE))
        print(f"{self.faulty_service} now runs with {self.SYSCTL}={self.BAD_RANGE}")

    @mark_fault_injected
    def recover_fault(self):
        self.kubectl.exec_command_checked(
            f"kubectl patch deployment {self.faulty_service} -n {self.namespace} --type=merge "
            f"-p {shlex.quote(json.dumps(self._sysctl_patch(None)))}"
        )
        rollout_status(self, self.faulty_service)

    def fault_check(self, oracle) -> dict | None:
        selector = selector_of(self, self.faulty_service)
        pods = self.kubectl.core_v1_api.list_namespaced_pod(self.namespace, label_selector=selector).items
        running = [p for p in pods if p.status.phase == "Running" and not p.metadata.deletion_timestamp]
        if not running:
            return oracle.fail("no_pods_found", deployment=self.faulty_service)
        for pod in running:
            value = self.app.exec_in(f"pod/{pod.metadata.name}", f"cat /proc/sys/{self.SYSCTL.replace('.', '/')}")
            low, high = (int(v) for v in value.split())
            print(f"{pod.metadata.name}: {self.SYSCTL} = {low} {high}")
            if high - low + 1 < self.MIN_PORTS:
                print(f"❌ {pod.metadata.name} has only {high - low + 1} ephemeral ports")
                return oracle.fail("fault_still_present", pod=pod.metadata.name, port_range=f"{low} {high}")
        return None


class NodeConntrackExhaustionFrappe(NodeConntrackExhaustionHotelReservation):
    """Conntrack flood pinned to the node that runs gunicorn (and, via the RWO volume, the bench)."""

    def __init__(self, app_name: str = APP, frontend: str = GUNICORN):
        super().__init__(app=make_app(app_name))
        self.frontend_deployment = frontend
        self.victim_node = self.gateway_node = None
        self.original_conntrack_max = self.target_connections = None
        self.conntrack_max_changed = False
        configure_port(
            self,
            app_name,
            component=(
                f"victim node nf_conntrack table + deployment/{self.client_deployment} + "
                f"deployment/{self.gateway_deployment}"
            ),
            description=(
                f"The `{self.client_deployment}` deployment is pinned to the worker node that runs `{frontend}` (the "
                f"Frappe web tier behind `{WEB_SERVICE}`) and opens many held TCP connections to the "
                f"`{self.gateway_deployment}` service/deployment. They saturate that node's Linux nf_conntrack table, "
                "so new connections to and from the node (gunicorn's per-request MariaDB/Redis connections and the "
                "users' requests) time out or are dropped, even though Deployments, Pods, Services and Endpoints "
                "look healthy."
            ),
            oracle_factory=lambda problem: ConntrackMitigationOracle(
                problem, probe_url=f"http://{WEB_SERVICE}:8000/api/method/ping"
            ),
        )
        self.apps_v1 = client.AppsV1Api()
        self.core_v1 = client.CoreV1Api()

    def _frontend_node(self):
        pods = self.core_v1.list_namespaced_pod(
            self.namespace, label_selector=selector_of(self, self.frontend_deployment)
        ).items
        for pod in pods:
            if pod.status.phase == "Running" and pod.spec.node_name:
                return pod.spec.node_name


class LoadGeneratorFloodHomepageFrappe(Problem):
    """An in-namespace traffic client whose feature flag is flipped to flood the web tier.

    The original flipped ``loadGeneratorFloodHomepage`` in the demo's flagd
    ConfigMap so its load generator hammered the frontend's homepage. Frappe's
    own load generator cannot be reconfigured that way (it is the measurement),
    so the port runs a site warm-up client (``erp-site-warmer``) that reads the
    same kind of flag file from ConfigMap ``erp-site-warmer-flags``: off, it
    fetches the homepage once every 30 s; on, it floods it with concurrent
    requests and saturates gunicorn's 8 worker threads.
    """

    DEPLOYMENT = "erp-site-warmer"
    CONFIGMAP = "erp-site-warmer-flags"
    FLAG = "floodHomepage"
    CONCURRENCY = 320
    IMAGE = "busybox:1.36"

    def __init__(self, app_name: str = APP):
        super().__init__(app=make_app(app_name))
        self.faulty_service = WEB_SERVICE
        configure_port(
            self,
            app_name,
            component=f"deployment/{self.DEPLOYMENT} (configmap/{self.CONFIGMAP} flag {self.FLAG})",
            description=(
                f"The web tier (`{GUNICORN}`, Service `{WEB_SERVICE}`) is saturated by a sustained traffic surge on the "
                "homepage route, so users' Desk/API requests queue behind it and time out or fail. Mechanism: the "
                f'`{self.CONFIGMAP}` ConfigMap has the `{self.FLAG}` feature flag\'s `defaultVariant` set to `"on"`, '
                f"which makes the in-namespace `{self.DEPLOYMENT}` client switch from one homepage fetch every 30 s to "
                f"{self.CONCURRENCY} concurrent request loops against `http://{WEB_SERVICE}:8000/`. Fix: turn the flag "
                "off (or stop the client)."
            ),
            oracle_factory=FrappeFaultStateOracle,
        )

    def _flags(self, variant: str) -> str:
        return json.dumps(
            {
                "flags": {
                    self.FLAG: {
                        "description": "Hammer the homepage to warm every cache layer",
                        "state": "ENABLED",
                        "variants": {"on": True, "off": False},
                        "defaultVariant": variant,
                    }
                }
            },
            indent=2,
        )

    def _client(self) -> dict:
        script = textwrap.dedent(
            f"""\
            flag_on() {{ tr -d ' \\n' < /flags/flags.json | grep -o '"{self.FLAG}":.*' | grep -q '"defaultVariant":"on"'; }}
            hit() {{ wget -q -T 60 -O /dev/null "$TARGET" 2>/dev/null; }}
            while true; do
              if flag_on; then
                for i in $(seq 1 {self.CONCURRENCY}); do
                  ( while flag_on; do for j in 1 2 3 4 5 6 7 8 9 10; do hit; done; done ) &
                done
                wait
              else
                hit; sleep 30
              fi
            done
            """
        )
        labels = {"app.kubernetes.io/name": self.DEPLOYMENT, "app.kubernetes.io/component": "site-warmer"}
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": self.DEPLOYMENT, "namespace": self.namespace, "labels": labels},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "terminationGracePeriodSeconds": 1,
                        "containers": [
                            {
                                "name": "warmer",
                                "image": self.IMAGE,
                                "command": ["sh", "-c", script],
                                "env": [{"name": "TARGET", "value": f"http://{WEB_SERVICE}:8000/"}],
                                "volumeMounts": [{"name": "flags", "mountPath": "/flags"}],
                                "resources": {
                                    "requests": {"cpu": "100m", "memory": "256Mi"},
                                    "limits": {"memory": "768Mi"},
                                },
                            }
                        ],
                        "volumes": [{"name": "flags", "configMap": {"name": self.CONFIGMAP}}],
                    },
                },
            },
        }

    def _set_flag(self, variant: str) -> None:
        apply_manifest(
            self,
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": self.CONFIGMAP, "namespace": self.namespace},
                "data": {"flags.json": self._flags(variant)},
            },
        )

    @mark_fault_injected
    def inject_fault(self):
        self._set_flag("on")
        apply_manifest(self, self._client())
        rollout_status(self, self.DEPLOYMENT, timeout_s=300)
        print(f"{self.DEPLOYMENT} floods {WEB_SERVICE} ({self.FLAG}=on)")

    @mark_fault_injected
    def recover_fault(self):
        self.kubectl.exec_command(
            f"kubectl delete deployment {self.DEPLOYMENT} -n {self.namespace} --ignore-not-found --wait=true"
        )
        self.kubectl.exec_command(f"kubectl delete configmap {self.CONFIGMAP} -n {self.namespace} --ignore-not-found")

    def fault_check(self, oracle) -> dict | None:
        try:
            dep = self.kubectl.apps_v1_api.read_namespaced_deployment(self.DEPLOYMENT, self.namespace)
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                print(f"✅ {self.DEPLOYMENT} is gone")
                return None
            raise
        if not (dep.spec.replicas or 0):
            print(f"✅ {self.DEPLOYMENT} is scaled to 0")
            return None
        try:
            cm = self.kubectl.core_v1_api.read_namespaced_config_map(self.CONFIGMAP, self.namespace)
            flags = json.loads((cm.data or {}).get("flags.json") or "{}")
            variant = flags.get("flags", {}).get(self.FLAG, {}).get("defaultVariant")
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise
            variant = None
        if variant == "on":
            print(f"❌ {self.FLAG} is still on and {self.DEPLOYMENT} is running")
            return oracle.fail("fault_still_present", configmap=self.CONFIGMAP, flag=self.FLAG)
        print(f"✅ {self.FLAG} is {variant!r}")
        return None


class ExpiredTlsFrappe(ExpiredTlsHotelReservation):
    """The Frappe Ingress (to the ``erp`` nginx edge) serves an expired certificate.

    Like the original, the fault lives at the Ingress: pods stay healthy, and
    the chart's load generator talks to ``svc-frappe-web`` inside the cluster
    (as Hotel Reservation's wrk2 hit the frontend Service), so the load
    generator does not see it; HTTPS clients of the Ingress host do.
    """

    HOST = "erp.frappe.local"

    def __init__(self, app_name: str = APP):
        super().__init__(app=make_app(app_name))
        self.problem_id = "expired_tls_frappe"
        self.secret_name = "erp-tls"
        self.ingress_name = "erp-ingress"
        self.faulty_service = ["erp"]
        configure_port(
            self,
            app_name,
            component=self.ingress_name,
            description=(
                f"The Frappe Ingress `{self.ingress_name}` (host `{self.HOST}`, backend Service `erp`, the nginx "
                f"edge in front of gunicorn) is configured with TLS secret `{self.secret_name}`, which contains an "
                "expired certificate, so HTTPS connections to the site through the ingress controller fail "
                "certificate validation. Pods, Services and endpoints are all healthy. Fix: replace the secret with "
                "a valid certificate (or remove the TLS reference)."
            ),
            oracle_factory=lambda problem: ExpiredTlsWithProbeOracle(problem),
        )

    def _ingress(self) -> dict:
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {
                "name": self.ingress_name,
                "namespace": self.namespace,
                "annotations": {
                    "nginx.ingress.kubernetes.io/ssl-redirect": "true",
                    # Frappe picks the site by Host header.
                    "nginx.ingress.kubernetes.io/upstream-vhost": WEB_SERVICE,
                },
            },
            "spec": {
                "ingressClassName": "nginx",
                "tls": [{"hosts": [self.HOST], "secretName": self.secret_name}],
                "rules": [
                    {
                        "host": self.HOST,
                        "http": {
                            "paths": [
                                {
                                    "path": "/",
                                    "pathType": "Prefix",
                                    "backend": {"service": {"name": "erp", "port": {"number": 8080}}},
                                }
                            ]
                        },
                    }
                ],
            },
        }

    @mark_fault_injected
    def inject_fault(self):
        IngressNginx().deploy()
        cert_pem, key_pem = self._generate_expired_cert_for(self.HOST)
        self._create_tls_secret(cert_pem, key_pem)
        apply_manifest(self, self._ingress())
        print(f"Ingress {self.ingress_name} serves an expired certificate for {self.HOST}")

    @mark_fault_injected
    def recover_fault(self):
        self.kubectl.exec_command(f"kubectl delete ingress {self.ingress_name} -n {self.namespace} --ignore-not-found")
        self.kubectl.exec_command(f"kubectl delete secret {self.secret_name} -n {self.namespace} --ignore-not-found")

    @staticmethod
    def _generate_expired_cert_for(host: str):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
        now = datetime.datetime.now(datetime.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=10))
            .not_valid_after(now - datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .sign(key, hashes.SHA256())
        )
        return (
            cert.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
            ),
        )
