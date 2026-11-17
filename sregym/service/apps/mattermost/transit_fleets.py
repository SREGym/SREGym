"""Tier 2: two fleets behind the provider network, one provisioner, one instance quota.

Customers reach chat through an nginx edge fleet that terminates their TLS and
forwards to Mattermost across the provider network. Mattermost notifies people
away from their desks through a fleet of Mattermost push relays, whose sends to
Firebase cross the same network. Both fleets are sized by the same capacity
scaler, with the same two policies each, and every new instance of either
registers with the same provisioner and takes a place under the same provider
quota. Tier 1b's shortcuts stay closed: in-house programs ship in images, and
Mattermost accepts connections only from the provider network.
"""

import base64
import datetime
import json
import subprocess
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from sregym.service.apps.mattermost import in_house, push_proxy
from sregym.service.apps.mattermost.application import MattermostApplication
from sregym.service.apps.mattermost.cascade import CONTROL_VOLUME, MattermostCascadeApplication
from sregym.service.apps.mattermost.in_house import InHouseImage
from sregym.service.apps.mattermost.transit_cascade import (
    INSTANCE_QUOTA,
    PROVIDER_NAMESPACE,
    RUNTIME_IMAGE,
    TRANSIT_PORT,
    MattermostTransitPackagedApplication,
    composed_source,
    still_provisioning,
)

NGINX_IMAGE = "nginx:1.26.3@sha256:41b194461e4bae16f9b25d68b0976ed4735b89ca625c89aad88e1c1c3b7e8860"
EDGE_PORT = 8443
STATUS_PORT = 8080
#: Requests each edge instance processes at once; beyond it, nginx answers 503.
EDGE_CONNECTIONS = 8
#: Connections the provider lets each instance hold across the transit hop at once.
INSTANCE_FLOWS = EDGE_CONNECTIONS
#: Seconds a connection over that limit waits for a slot before it is reset: only
#: long enough to cover a slot being released, so extra connections are refused,
#: not queued.
INSTANCE_WAIT = 0.01
#: The customer's instances, as the provider's inventory and network find them.
FLEET_SELECTOR = "app in (chat-gateway,push-proxy)"
#: Sends in flight one push relay instance is sized for, and the connections the
#: provider lets it hold across the transit hop to Firebase at once.
PUSH_SEND_BUDGET = 4
RECEIVER_PORT = 8080
#: Transit listener for the push relay's Firebase traffic (a kept-alive stream).
FIREBASE_TRANSIT_PORT = 8443
FIREBASE_PROJECT = "chat-mobile-4f2a1"
CHAT_FLEET_MAX = 20
PUSH_FLEET_MAX = 10
PUSH_REPLICAS = 2
#: Seconds a launched instance boots before it can be configured, holding its
#: place under the quota all the while.
INSTANCE_BOOT_SECONDS = 60
#: Each scale-out adds this share of the fleet's current size, and at least its step.
SCALE_OUT_PERCENT = 100
#: People on phones, people at desks, and conversations per desk.
PHONES, DESKS, CONVERSATIONS = 40, 10, 4
#: Direct messages per second to people on phones at healthy traffic; they rise
#: and fall with the customers' rate.
MESSAGE_RATE = 5
#: Provider programs that list the customer's instances.
INVENTORY_READERS = ("compute", "transit")
EDGE_NAMES = ("chat-gateway", "chat-gateway.mattermost.svc", "chat-gateway.mattermost.svc.cluster.local")

CATALOG_DESCRIPTION = (
    " Services in this deployment: `chat-gateway`, an nginx fleet that terminates customers' TLS and forwards"
    " chat traffic to Mattermost; `mattermost`, the chat backend, with its PostgreSQL cluster; `push-proxy`, a"
    " fleet of Mattermost push proxies that deliver mobile notifications through Firebase Cloud Messaging;"
    " `provisioner`, the in-house service new instances of either fleet register with before they take"
    " traffic; and `capacity-scaler`, the in-house autoscaler that sets both fleets' replica counts. Mattermost"
    " and Firebase are reached through a cloud provider's managed network, which is not under your"
    " administration. Metrics, logs and traces are collected by the cluster's observability stack."
)


def nginx_conf(upstream, connections=EDGE_CONNECTIONS):
    return f"""worker_processes 1;
error_log /dev/stderr warn;
pid /tmp/nginx.pid;

events {{
    worker_connections 1024;
}}

http {{
    access_log /dev/stdout combined;
    limit_conn_zone $server_name zone=edge:1m;
    limit_conn_status 503;
    limit_conn_log_level warn;

    upstream chat_backend {{
        server {upstream};
    }}

    server {{
        listen {EDGE_PORT} ssl;
        server_name {EDGE_NAMES[0]};
        ssl_certificate /etc/nginx/tls/tls.crt;
        ssl_certificate_key /etc/nginx/tls/tls.key;
        client_max_body_size 50m;
        limit_conn edge {connections};

        location / {{
            proxy_pass http://chat_backend;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto https;
            proxy_connect_timeout 15s;
            proxy_read_timeout 20s;
        }}
    }}

    server {{
        listen {STATUS_PORT};
        location = /nginx_status {{
            stub_status;
        }}
        location = /health {{
            return 200 "ok\\n";
        }}
    }}
}}
"""


def self_signed(names):
    """``(key PEM, certificate PEM)`` for the edge's own hostnames."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name) for name in names]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return key_pem, cert.public_bytes(serialization.Encoding.PEM).decode()


def firebase_service_account(project_id, token_uri):
    """The push relay's Firebase credentials, with a fresh key, pointing at the stand-in's token endpoint."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return json.dumps(
        {
            "type": "service_account",
            "project_id": project_id,
            "private_key_id": "1",
            "private_key": key_pem,
            "client_email": f"push-relay@{project_id}.iam.gserviceaccount.com",
            "client_id": "100000000000000000001",
            "token_uri": token_uri,
        },
        indent=2,
    )


class MattermostTransitFleetsApplication(MattermostTransitPackagedApplication):
    cascade_deployments = ("chat-gateway", "push-proxy", "capacity-scaler", "provisioner")
    instance_quota = INSTANCE_QUOTA
    capacity_groups = ("chat-gateway", "push-proxy")
    gateway_port = EDGE_PORT
    states_capacity_requirement = False
    fleet_max = CHAT_FLEET_MAX
    push_fleet_max = PUSH_FLEET_MAX
    message_rate = MESSAGE_RATE

    @property
    def groups(self):
        return [
            {"name": "chat-gateway", "kind": "proxy", "port": STATUS_PORT, "limit": EDGE_CONNECTIONS},
            {"name": "push-proxy", "kind": "push", "port": push_proxy.PORT, "limit": PUSH_SEND_BUDGET},
        ]

    @property
    def firebase_url(self):
        return f"http://transit.{PROVIDER_NAMESPACE}.svc:{FIREBASE_TRANSIT_PORT}"

    @property
    def compute_url(self):
        return f"http://compute.{PROVIDER_NAMESPACE}.svc:8080/instances"

    def get_app_json(self):
        result = MattermostApplication.get_app_json(self)
        result["Desc"] += CATALOG_DESCRIPTION
        return result

    # ---- images ------------------------------------------------------------

    def in_house_images(self):
        def raw(name):
            return MattermostCascadeApplication.runtime_source(self, name)

        bootstrap = InHouseImage("platform/instance-bootstrap", {"register.py": self.registration_source()})
        provisioner = InHouseImage(
            "platform/provisioner",
            {
                "provisioner.py": composed_source(
                    composed_source(raw("provisioner.py"), raw("quota_provisioner.py")),
                    raw("provisioner_service.py"),
                ),
                "start": "#!/bin/sh\n"
                f'ulimit -n "${{MAX_OPEN_FILES:-{self.provisioner_open_files}}}"\n'
                "exec python -u /srv/provisioner.py\n",
            },
            executables=("start",),
        )
        scaler = InHouseImage(
            "platform/capacity-scaler",
            {
                "scaler.py": composed_source(
                    composed_source(
                        composed_source(raw("capacity_scaler.py"), raw("fleet_scaler.py")), raw("activity_history.py")
                    ),
                    raw("fleet_groups.py"),
                )
            },
        )
        return [bootstrap, provisioner, scaler, push_proxy.image()]

    # ---- provider side (hidden) ------------------------------------------

    def provider_documents(self):
        state = [{"name": "state", "emptyDir": {}}]
        transit = self.deployment(
            "transit",
            {
                "name": "transit",
                "image": RUNTIME_IMAGE,
                "command": ["python", "-u", "-c", self.runtime_source("transit.py")],
                "env": [
                    {
                        "name": "TRANSIT_ROUTES",
                        "value": f"{TRANSIT_PORT}=mattermost.{self.namespace}.svc:8065,"
                        f"{FIREBASE_TRANSIT_PORT}=push-receiver.{PROVIDER_NAMESPACE}.svc:{RECEIVER_PORT}/stream",
                    },
                    {"name": "TRANSIT_CAPACITY", "value": str(self.transit_capacity)},
                    {"name": "TRANSIT_IMPAIRED_LATENCY", "value": str(self.transit_impaired_latency)},
                    {"name": "TRANSIT_IMPAIRED_LOSS", "value": str(self.transit_impaired_loss)},
                    {
                        "name": "TRANSIT_INSTANCE_FLOWS",
                        "value": f"{TRANSIT_PORT}={INSTANCE_FLOWS},{FIREBASE_TRANSIT_PORT}={PUSH_SEND_BUDGET}",
                    },
                    {"name": "TRANSIT_INSTANCE_NAMESPACE", "value": self.namespace},
                    {"name": "TRANSIT_INSTANCE_SELECTOR", "value": FLEET_SELECTOR},
                    {"name": "TRANSIT_INSTANCE_WAIT", "value": str(INSTANCE_WAIT)},
                ],
                "volumeMounts": [{"name": "state", "mountPath": "/run/transit"}],
                "resources": {"requests": {"cpu": "200m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
            },
            state,
        )
        receiver = self.deployment(
            "push-receiver",
            {
                "name": "receiver",
                "image": RUNTIME_IMAGE,
                "command": ["python", "-u", "-c", self.runtime_source("push_receiver.py")],
                "volumeMounts": [{"name": "state", "mountPath": "/run/push"}],
                "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
            },
            state,
        )
        accounts = [
            {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": name}} for name in INVENTORY_READERS
        ]
        compute = self.deployment(
            "compute",
            {
                "name": "compute",
                "image": RUNTIME_IMAGE,
                "command": ["python", "-u", "-c", self.runtime_source("compute.py")],
                "env": [
                    {"name": "COMPUTE_QUOTA", "value": str(self.instance_quota)},
                    {"name": "COMPUTE_NAMESPACE", "value": self.namespace},
                    {"name": "COMPUTE_SELECTOR", "value": FLEET_SELECTOR},
                    {"name": "COMPUTE_BOOT_SECONDS", "value": str(INSTANCE_BOOT_SECONDS)},
                ],
                "volumeMounts": [{"name": "state", "mountPath": "/run/compute"}],
                "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
            },
            state,
        )
        for name, deployment in zip(INVENTORY_READERS, (compute, transit), strict=True):
            deployment["spec"]["template"]["spec"]["serviceAccountName"] = name
            deployment["spec"]["template"]["spec"]["automountServiceAccountToken"] = True
        return [
            transit,
            self.transit_service(),
            receiver,
            self.service("push-receiver", RECEIVER_PORT),
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": "push-receiver-ingress"},
                "spec": {
                    "podSelector": {"matchLabels": {"app": "push-receiver"}},
                    "policyTypes": ["Ingress"],
                    "ingress": [
                        {
                            "from": [{"podSelector": {"matchLabels": {"app": "transit"}}}],
                            "ports": [{"protocol": "TCP", "port": RECEIVER_PORT}],
                        }
                    ],
                },
            },
            *accounts,
            compute,
            self.service("compute", 8080),
        ]

    def transit_service(self):
        """The transit Service with both listeners."""
        service = self.service("transit", TRANSIT_PORT)
        service["spec"]["ports"] = [
            {"name": "chat", "port": TRANSIT_PORT, "targetPort": TRANSIT_PORT},
            {"name": "firebase", "port": FIREBASE_TRANSIT_PORT, "targetPort": FIREBASE_TRANSIT_PORT},
        ]
        return service

    def customers_document(self):
        customers = self.deployment(
            "customers",
            {
                "name": "customers",
                "image": RUNTIME_IMAGE,
                "command": ["python", "-u", "-c", self.runtime_source("chat_customers.py")],
                "volumeMounts": [
                    {"name": "state", "mountPath": "/run/customers"},
                    {"name": "workload", "mountPath": "/etc/customers", "readOnly": True},
                ],
                "resources": {"requests": {"cpu": "300m", "memory": "128Mi"}, "limits": {"memory": "768Mi"}},
            },
            [{"name": "state", "emptyDir": {}}, {"name": "workload", "secret": {"secretName": "customer-workload"}}],
        )
        return customers

    # ---- application side (what the responder sees) ---------------------

    def mattermost_documents(self):
        documents = MattermostApplication.application_documents(self)
        for document in documents:
            if document["kind"] == "Deployment" and document["metadata"]["name"] == self.slug:
                container = document["spec"]["template"]["spec"]["containers"][0]
                container["env"] += [
                    {"name": "MM_EMAILSETTINGS_SENDPUSHNOTIFICATIONS", "value": "true"},
                    {
                        "name": "MM_EMAILSETTINGS_PUSHNOTIFICATIONSERVER",
                        "value": f"http://push-proxy:{push_proxy.PORT}",
                    },
                    {"name": "MM_EMAILSETTINGS_PUSHNOTIFICATIONCONTENTS", "value": "full"},
                ]
        return documents

    def register_init(self, image):
        return {
            "name": "register",
            "image": image,
            "imagePullPolicy": "Never",
            "command": ["python", "-u", "/srv/register.py"],
            "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"memory": "64Mi"}},
        }

    def scaler_rbac(self):
        account, role, binding = super().scaler_rbac()
        role["rules"][1]["resourceNames"] = [group["name"] for group in self.groups]
        role["rules"].append({"apiGroups": ["metrics.k8s.io"], "resources": ["pods"], "verbs": ["list", "get"]})
        return [account, role, binding]

    def inventory_rbac(self):
        role = {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": "compute-inventory"},
            "rules": [{"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]}],
        }
        binding = {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": "compute-inventory"},
            "subjects": [
                {"kind": "ServiceAccount", "name": name, "namespace": PROVIDER_NAMESPACE} for name in INVENTORY_READERS
            ],
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "compute-inventory"},
        }
        return [role, binding]

    def application_documents(self):
        bootstrap, provisioner_image, scaler_image, relay_image = (i.reference for i in self.in_house_images())
        control = [{"name": "control", "persistentVolumeClaim": {"claimName": CONTROL_VOLUME}}]

        edge = self.deployment(
            "chat-gateway",
            {
                "name": "nginx",
                "image": NGINX_IMAGE,
                "ports": [{"containerPort": EDGE_PORT}, {"containerPort": STATUS_PORT}],
                "volumeMounts": [
                    {"name": "config", "mountPath": "/etc/nginx/nginx.conf", "subPath": "nginx.conf"},
                    {"name": "tls", "mountPath": "/etc/nginx/tls", "readOnly": True},
                ],
                "readinessProbe": {"httpGet": {"path": "/health", "port": STATUS_PORT}, "periodSeconds": 5},
                "resources": {"requests": {"cpu": "100m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}},
            },
            [
                {"name": "config", "configMap": {"name": "chat-gateway-nginx"}},
                {"name": "tls", "secret": {"secretName": "chat-gateway-tls"}},
            ],
            initContainers=[self.register_init(bootstrap)],
        )
        edge["spec"]["replicas"] = self.capacity_floor
        edge_config = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "chat-gateway-nginx"},
            "data": {"nginx.conf": nginx_conf(self.transit_url.removeprefix("http://"))},
        }

        relay = self.deployment(
            "push-proxy",
            {
                "name": "push-proxy",
                "image": relay_image,
                "imagePullPolicy": "Never",
                "ports": [{"containerPort": push_proxy.PORT}],
                "env": [{"name": "FCM_ENDPOINT", "value": f"{self.firebase_url}/v1"}],
                "volumeMounts": [
                    {"name": "config", "mountPath": "/mattermost-push-proxy/config", "readOnly": True},
                    {"name": "firebase", "mountPath": "/mattermost-push-proxy/certs", "readOnly": True},
                ],
                "readinessProbe": {"tcpSocket": {"port": push_proxy.PORT}, "periodSeconds": 5},
                "resources": {"requests": {"cpu": "50m", "memory": "32Mi"}, "limits": {"memory": "256Mi"}},
            },
            [
                {"name": "config", "configMap": {"name": "push-proxy-config"}},
                {"name": "firebase", "secret": {"secretName": "push-proxy-firebase"}},
            ],
            initContainers=[self.register_init(bootstrap)],
        )
        relay["spec"]["replicas"] = PUSH_REPLICAS
        relay_config = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "push-proxy-config"},
            "data": {"mattermost-push-proxy.json": push_proxy.config()},
        }

        provisioner = self.deployment(
            "provisioner",
            {
                "name": "provisioner",
                "image": provisioner_image,
                "imagePullPolicy": "Never",
                "command": ["/srv/start"],
                "env": [
                    {"name": "PROVISIONER_BACKEND", "value": f"{self.transit_url}/api/v4/system/ping"},
                    {"name": "PROVISIONER_FETCHES", "value": str(self.provisioner_fetches)},
                    {"name": "PROVISIONER_COMPUTE", "value": self.compute_url},
                ],
                "volumeMounts": [{"name": "data", "mountPath": "/var/lib/provisioner"}],
                "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
            },
            [{"name": "data", "emptyDir": {}}],
        )

        scaler = self.deployment(
            "capacity-scaler",
            {
                "name": "scaler",
                "image": scaler_image,
                "imagePullPolicy": "Never",
                "command": ["python", "-u", "/srv/scaler.py"],
                "env": [
                    {"name": "NAMESPACE", "value": self.namespace},
                    {"name": "SCALER_GROUPS", "value": json.dumps(self.groups)},
                    {"name": "SCALER_INTERVAL", "value": str(self.scaler_interval)},
                ],
                "volumeMounts": [{"name": "control", "mountPath": "/control"}],
                "readinessProbe": {"httpGet": {"path": "/health", "port": 8080}, "periodSeconds": 5},
                "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
            },
            control,
        )
        scaler["spec"]["template"]["spec"]["serviceAccountName"] = "capacity-scaler"
        scaler["spec"]["template"]["spec"]["automountServiceAccountToken"] = True

        edge_service = self.service("chat-gateway", EDGE_PORT)
        return [
            *self.mattermost_documents(),
            *self.scaler_rbac(),
            *self.inventory_rbac(),
            edge_config,
            relay_config,
            edge_service,
            self.service("push-proxy", push_proxy.PORT),
            self.service("capacity-scaler", 8080),
            self.service("provisioner", 8080),
            edge,
            relay,
            provisioner,
            scaler,
            self.backend_ingress(),
        ]

    # ---- capacity policy -------------------------------------------------

    def write_control(self, name, content):
        """Write through the scaler pod: here only it mounts the control volume."""
        self.command(
            "exec",
            "-i",
            "deployment/capacity-scaler",
            "--",
            "sh",
            "-c",
            'cat > "/control/$1"',
            "write-control",
            name,
            input_text=content,
        )

    def read_control(self, name):
        return self.command("exec", "deployment/capacity-scaler", "--", "cat", f"/control/{name}")

    def default_policy(self, group="chat-gateway"):
        policy = super().default_policy()
        policy["scale_out"]["percent"] = SCALE_OUT_PERCENT
        if group == "push-proxy":
            policy.update(min=1, max=self.push_fleet_max)
        return policy

    @staticmethod
    def policy_file(group):
        return "scaler.json" if group == "chat-gateway" else f"scaler-{group}.json"

    def current_policy(self, group="chat-gateway"):
        try:
            return json.loads(self.read_control(self.policy_file(group)))
        except Exception:
            return self.default_policy(group)

    def scaler_policy(self, group="chat-gateway", **changes):
        policy = self.current_policy(group)
        for key, value in changes.items():
            policy[key] = {**policy[key], **value} if isinstance(value, dict) and key in policy else value
        self.write_control(self.policy_file(group), json.dumps(policy, indent=2) + "\n")
        return policy

    def group_cpu(self, group):
        """Mean percent of one core over the group's pods, from the metrics API."""
        raw = self.command(
            "get", "--raw", f"/apis/metrics.k8s.io/v1beta1/namespaces/{self.namespace}/pods?labelSelector=app%3D{group}"
        )
        values = []
        for item in json.loads(raw).get("items", []):
            cores = 0.0
            for container in item.get("containers", []):
                quantity = container["usage"]["cpu"]
                units = {"n": 1e-9, "u": 1e-6, "m": 1e-3}
                cores += float(quantity[:-1]) * units[quantity[-1]] if quantity[-1] in units else float(quantity)
            values.append(100.0 * cores)
        return round(sum(values) / len(values), 2) if values else None

    def healthy_cpu(self, group="chat-gateway", samples=3, pause=20):
        readings = []
        for index in range(samples):
            if index:
                time.sleep(pause)
            if (value := self.group_cpu(group)) is not None:
                readings.append(value)
        if not readings:
            raise RuntimeError(f"Could not measure {group} CPU from the metrics API")
        return round(sum(readings) / len(readings), 2)

    def calibrate_scaler(self):
        """Each group's scale-in threshold from its own measured healthy CPU."""
        policies = {}
        for group in (g["name"] for g in self.groups):
            healthy = self.healthy_cpu(group)
            self.write_control(self.policy_file(group), json.dumps(self.default_policy(group), indent=2) + "\n")
            policies[group] = self.scaler_policy(
                group, scale_in={"below": max(0.5, round(healthy * 0.6, 2))}, calibrated_healthy_cpu_percent=healthy
            )
        return policies

    # ---- lifecycle -------------------------------------------------------

    def ensure_secret(self, name, data, provider=False):
        target = self.provider if provider else self
        if not target.command("get", "secret", name, "--ignore-not-found", "-o", "name").strip():
            target.apply([{"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name}, "stringData": data}])

    def edge_certificate(self):
        """The edge's certificate, created once and kept for the deployment's life."""
        if not self.command("get", "secret", "chat-gateway-tls", "--ignore-not-found", "-o", "name").strip():
            key, cert = self_signed(EDGE_NAMES)
            self.apply(
                [
                    {
                        "apiVersion": "v1",
                        "kind": "Secret",
                        "metadata": {"name": "chat-gateway-tls"},
                        "type": "kubernetes.io/tls",
                        "stringData": {"tls.crt": cert, "tls.key": key},
                    }
                ]
            )
        return self.command("get", "secret", "chat-gateway-tls", "-o", "jsonpath={.data.tls\\.crt}")

    def seed_workload(self):
        source = MattermostCascadeApplication.runtime_source(self, "workload_seed.py")
        args = [str(PHONES), str(DESKS), str(CONVERSATIONS), f"https://chat-gateway.{self.namespace}.svc:{EDGE_PORT}"]
        output = self.command(
            "exec", "-i", "application-client", "--", "python", "-", *args, input_text=source, timeout=600
        )
        return json.loads(output.strip().splitlines()[-1])

    def deploy(self):
        absent = in_house.missing(self.in_house_images(), in_house.node_images())
        if absent:
            raise RuntimeError(
                f"In-house images are not on the node: {absent}. Build them on each node with "
                "`uv run python -m sregym.service.apps.mattermost.in_house --tier 2`."
            )
        if not self.provider_exists():
            subprocess.run(["kubectl", "create", "namespace", PROVIDER_NAMESPACE], check=True, capture_output=True)
        self.provider.apply(self.provider_documents())
        for name in ("transit", "push-receiver", "compute"):
            self.provider.command("rollout", "status", f"deployment/{name}", "--timeout=300s", timeout=330)

        self.create_namespace()
        edge_cert = base64.b64decode(self.edge_certificate()).decode()
        self.ensure_secret(
            "push-proxy-firebase",
            {"firebase.json": firebase_service_account(FIREBASE_PROJECT, f"{self.firebase_url}/token")},
        )
        MattermostApplication.deploy(self)
        # Instances started registering before Mattermost was up, and every
        # attempt that failed partway kept its descriptors; start the
        # provisioner clean now that the backend answers.
        self.command("rollout", "restart", "deployment/provisioner")
        for name in self.cascade_deployments:
            self.command("rollout", "status", f"deployment/{name}", "--timeout=300s", timeout=330)

        workload = self.seed_workload()
        self.provider.apply(
            [
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": "customer-workload"},
                    "stringData": {"workload.json": json.dumps(workload), "ca.pem": edge_cert},
                },
                self.customers_document(),
            ]
        )
        self.provider.command("rollout", "status", "deployment/customers", "--timeout=300s", timeout=330)
        self.set_customer_rate(self.healthy_rate)
        if not self.command("get", "configmap", "capacity-calibrated", "--ignore-not-found", "-o", "name").strip():
            # Let the metrics API cover a few windows of healthy traffic.
            time.sleep(60)
            policies = self.calibrate_scaler()
            self.apply(
                [
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {"name": "capacity-calibrated"},
                        "data": {group: json.dumps(policy, indent=2) + "\n" for group, policy in policies.items()},
                    }
                ]
            )

    # ---- provider and customers (harness only) ---------------------------

    def set_customer_rate(self, per_second):
        super().set_customer_rate(per_second)
        self.set_message_rate(self.message_rate * per_second / self.healthy_rate)

    def set_message_rate(self, per_second):
        self.provider_write("customers", "/run/customers/message_rate", f"{float(per_second)}\n")

    def push_records(self, since):
        """Direct messages posted and notifications delivered at or after ``since``, on the provider's clock."""
        return (
            self.provider_records("customers", "/run/customers/posts.jsonl", since),
            self.provider_records("push-receiver", "/run/push/deliveries.jsonl", since),
        )

    def provider_records(self, deployment, path, since):
        script = (
            "import json,os,sys\n"
            "path,since=sys.argv[1],float(sys.argv[2])\n"
            "if os.path.exists(path):\n"
            " for line in open(path):\n"
            "  if line.strip() and json.loads(line)['at']>=since:\n"
            "   sys.stdout.write(line)\n"
        )
        output = self.provider.command(
            "exec", f"deployment/{deployment}", "--", "python", "-c", script, path, str(since), timeout=120
        )
        return [json.loads(line) for line in output.splitlines() if line.strip()]

    def compute_stats(self):
        return self.provider_read("compute", "/run/compute/stats.json")

    def fleet(self, group):
        """Requested replicas, pods in service, and pods still provisioning, for one group."""
        deployment = json.loads(self.command("get", "deployment", group, "-o", "json"))
        pods = json.loads(self.command("get", "pods", "-l", f"app={group}", "-o", "json"))["items"]
        live = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
        in_service = [
            p
            for p in live
            if p.get("status", {}).get("phase") == "Running"
            and any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions", []))
        ]
        return {
            "requested": deployment["spec"].get("replicas", 0),
            "in_service": len(in_service),
            "provisioning": len(still_provisioning(live)),
        }

    def probe_through_gateway(self, attempts=12):
        """Pings through the edge over TLS, from inside the cluster."""
        script = (
            "import json,ssl,time,urllib.request\n"
            "ctx=ssl._create_unverified_context()\n"
            "served=failed=shed=0\n"
            f"for _ in range({attempts}):\n"
            " try:\n"
            f"  with urllib.request.urlopen('https://chat-gateway:{EDGE_PORT}/api/v4/system/ping',timeout=30,"
            "context=ctx) as r:\n"
            "   served+=r.status==200\n"
            " except urllib.error.HTTPError as e:\n"
            "  shed+=e.code==503; failed+=e.code!=503\n"
            " except Exception:\n"
            "  failed+=1\n"
            "print(json.dumps({'served':served,'shed':shed,'failed':failed}))\n"
        )
        return json.loads(
            self.command("exec", "-i", "application-client", "--", "python", "-", input_text=script, timeout=180)
        )
