"""A hosted workspace: mail and chat behind a regional edge gateway fleet.

Tenant users reach both services through `edge-gw`, a fleet of gateway units
grouped into regions. A traffic manager probes every unit, ejects failing ones
and steers each region's traffic by labelling unit pods; the `mail-<region>`
and `chat-<region>` Services select on those labels, or on the smaller,
independent alternate gateway (`edge-alt`, HAProxy) when a service is placed
there.

Mail is real Postfix (transport, with a persistent queue) delivering over LMTP
through the gateway into Dovecot (mailboxes, IMAP). Chat is Mattermost from the
shared SaaS application, which also brings PostgreSQL and the shared
durability checks.

The user population, the external mail sender and the sink that receives
bounces run as `workspace-clients` in the hidden namespace, out of the agent's
view and reach.
"""

import base64
import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from sregym.service.apps.components.client_runner import PythonClientRunner
from sregym.service.apps.helpers import image_reference
from sregym.service.apps.mattermost.application import MattermostApplication
from sregym.service.apps.workspace.runtime import traffic_manager

#: Gateway releases in the order they were rolled out. Every release from
#: 2026.07 on admits sessions through the shared table; 2026.09 added the
#: tuning keys that the 2026.06 line rejects.
EDGE_RELEASES = (
    ("2026.06.4", image_reference("workspace-edge-2026.06.4")),
    ("2026.07.2", image_reference("workspace-edge-2026.07.2")),
    ("2026.09.1", image_reference("workspace-edge-2026.09.1")),
    ("2026.11.1", image_reference("workspace-edge")),
)
EDGE_IMAGE = EDGE_RELEASES[-1][1]
KNOWN_GOOD_RELEASE = EDGE_RELEASES[0]
MAIL_IMAGE = image_reference("workspace-mail")
HAPROXY_IMAGE = "haproxy:3.0.5-alpine"
PYTHON_IMAGE = "python:3.12.13-alpine3.23"

HIDDEN_NAMESPACE = "khaos"
CLIENTS = "workspace-clients"
REGIONS = ("a", "b", "c")
MAIL_DOMAIN = "tenant.example"
PARTNER_DOMAIN = "partners.example"
#: Postfix returns mail it could not deliver within this window.
MAIL_QUEUE_LIFETIME = "30m"

#: Per tier: gateway units per region, CPU per unit, mail accounts, chat users,
#: and the user population at the quiet baseline and at the daily peak, per
#: region. The baseline is far below a unit's limits; the peak only exceeds
#: them on the shared admission path once chat has failed over onto the fleet
#: and some capacity is out.
TIERS = {
    "single": {
        "units": 3,
        "unit_cpu": "150m",
        "accounts": 200,
        "chat_users": 40,
        "baseline": {"mail": 350, "chat": 100},
        "peak": {"mail": 700, "chat": 200},
    },
    "replicated": {
        "units": 5,
        "unit_cpu": "150m",
        "accounts": 250,
        "chat_users": 60,
        "baseline": {"mail": 580, "chat": 170},
        "peak": {"mail": 1170, "chat": 330},
    },
}

EDGE_ROUTES = """\
route = mail-imap :1143 mailbox:143
route = mail-lmtp :1024 mailbox:24
route = chat      :8065 mattermost:8065
"""

#: The unit configuration before 2026.09 introduced the tuning keys.
EDGE_CONFIG_2606 = (
    "# edge-gw unit configuration, read once at startup.\nunit = ${UNIT}\nregion = ${REGION}\n\n" + EDGE_ROUTES
)

EDGE_CONFIG = (
    "# edge-gw unit configuration, read once at startup.\n"
    "unit = ${UNIT}\n"
    "region = ${REGION}\n\n"
    "worker_threads = auto\n"
    "admission_timeout = 5s\n\n" + EDGE_ROUTES
)

HAPROXY_CONFIG = """\
# Alternate gateway: independent of edge-gw, sized for chat failover.
global
    maxconn 1200
    log stdout format raw local0 info

defaults
    mode tcp
    log global
    option tcplog
    timeout connect 3s
    timeout client 1h
    timeout server 1h
    timeout queue 5s

frontend stats
    mode http
    bind :8404
    http-request use-service prometheus-exporter if { path /metrics }
    stats enable
    stats uri /stats

frontend mail-imap
    bind :1143
    default_backend mailbox-imap
frontend mail-lmtp
    bind :1024
    default_backend mailbox-lmtp
frontend chat
    bind :8065
    default_backend chat

backend mailbox-imap
    server mailbox mailbox:143 maxconn 300
backend mailbox-lmtp
    server mailbox mailbox:24 maxconn 50
backend chat
    server mattermost mattermost:8065 maxconn 1100
"""


class WorkspaceApplication(MattermostApplication):
    app_id = "workspace"
    metadata_file = Path(__file__).with_name("metadata.json")
    client_workflow = Path(__file__).with_name("workflow.py")
    #: Waited on at deploy, but not held to one replica by the shared oracle:
    #: scaling the alternate gateway or the traffic manager is fair game.
    incident_deployments = ("traffic-manager", "edge-alt")
    #: Claims of the mailbox and transport StatefulSets.
    statefulset_volumes = ("mail-mailbox-0", "queue-mail-transport-0")

    def __init__(self, scale_tier="single"):
        super().__init__(scale_tier)
        self.tier = TIERS[scale_tier]

    # ---- documents ---------------------------------------------------------------

    def units(self, region):
        return [f"edge-gw-{region}-{i}" for i in range(self.tier["units"])]

    def all_units(self):
        return [u for r in REGIONS for u in self.units(r)]

    def config_map(self, name, data, namespace=None, labels=None):
        metadata = {"name": name, **({"namespace": namespace} if namespace else {})}
        if labels:
            metadata["labels"] = labels
        return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata, "data": data}

    def steered_service(self, service, region):
        ports = {"mail": [("imap", 1143), ("lmtp", 1024)], "chat": [("http", 8065)]}[service]
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": f"{service}-{region}", "labels": {"steering": "traffic-manager"}},
            "spec": {
                "selector": {"app": "edge-gw", f"steer-{region}": "in"},
                "ports": [{"name": n, "port": p, "targetPort": p} for n, p in ports],
            },
        }

    def gateway_statefulset(self, region, release=EDGE_RELEASES[-1]):
        version, image = release
        container = {
            "name": "edge-gw",
            "image": image,
            "args": ["-config", "/etc/edge-gw/edge-gw.conf"],
            "env": [
                {"name": "UNIT", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
                {"name": "REGION", "value": region},
            ],
            "ports": [
                {"name": "mail-imap", "containerPort": 1143},
                {"name": "mail-lmtp", "containerPort": 1024},
                {"name": "chat", "containerPort": 8065},
                {"name": "probe", "containerPort": 8081},
                {"name": "metrics", "containerPort": 9100},
            ],
            "volumeMounts": [{"name": "config", "mountPath": "/etc/edge-gw"}],
            # Liveness only: rotation belongs to the traffic manager.
            "livenessProbe": {
                "httpGet": {"path": "/livez", "port": 9100},
                "periodSeconds": 10,
                "timeoutSeconds": 5,
                "failureThreshold": 6,
            },
            "resources": {
                "requests": {"cpu": "100m", "memory": "64Mi"},
                "limits": {"cpu": self.tier["unit_cpu"], "memory": "384Mi"},
            },
        }
        name = f"edge-gw-{region}"
        return {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": {
                "name": name,
                "labels": {"app": "edge-gw", "region": region},
                "annotations": {"kubernetes.io/change-cause": f"release {version}"},
            },
            "spec": {
                "serviceName": name,
                "replicas": self.tier["units"],
                "podManagementPolicy": "Parallel",
                "selector": {"matchLabels": {"app": "edge-gw", "region": region}},
                "template": {
                    "metadata": {"labels": {"app": "edge-gw", "region": region}},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "terminationGracePeriodSeconds": 5,
                        "containers": [container],
                        "volumes": [{"name": "config", "configMap": {"name": "edge-gw"}}],
                    },
                },
            },
        }

    def mail_statefulset(self, name, role, port_specs, mounts, volumes, claim):
        return {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": {"name": name},
            "spec": {
                "serviceName": name,
                "replicas": 1,
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": {"app": name}},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "containers": [
                            {
                                "name": role,
                                "image": MAIL_IMAGE,
                                "args": [role],
                                "ports": [{"name": n, "containerPort": p} for n, p in port_specs],
                                "volumeMounts": mounts,
                                "readinessProbe": {"tcpSocket": {"port": port_specs[0][1]}, "periodSeconds": 5},
                                "resources": {
                                    "requests": {"cpu": "100m", "memory": "128Mi"},
                                    "limits": {"memory": "3Gi"},
                                },
                            }
                        ],
                        "volumes": volumes,
                    },
                },
                "volumeClaimTemplates": [
                    {
                        "metadata": {"name": claim},
                        "spec": {
                            "accessModes": ["ReadWriteOnce"],
                            "storageClassName": self.storage_class,
                            "resources": {"requests": {"storage": "2Gi"}},
                        },
                    }
                ],
            },
        }

    def plain_service(self, name, ports, selector=None):
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name},
            "spec": {
                "selector": selector or {"app": name},
                "ports": [{"name": n, "port": p, "targetPort": p} for n, p in ports],
            },
        }

    def traffic_manager_documents(self):
        rbac = [
            {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "traffic-manager"}},
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": {"name": "traffic-manager"},
                "rules": [
                    {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "patch"]},
                    {"apiGroups": [""], "resources": ["services"], "verbs": ["get", "patch"]},
                    {"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get"]},
                ],
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": {"name": "traffic-manager"},
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "traffic-manager"},
                "subjects": [{"kind": "ServiceAccount", "name": "traffic-manager"}],
            },
        ]
        deployment = self.deployment(
            "traffic-manager",
            {
                "name": "traffic-manager",
                "image": PYTHON_IMAGE,
                "command": ["python", "-u", "-c", Path(traffic_manager.__file__).read_text()],
                "ports": [{"name": "metrics", "containerPort": 8080}],
                "livenessProbe": {"httpGet": {"path": "/health", "port": 8080}, "periodSeconds": 10},
                "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
            },
            serviceAccountName="traffic-manager",
        )
        deployment["spec"]["template"]["spec"]["automountServiceAccountToken"] = True
        return [*rbac, deployment, self.plain_service("traffic-manager", [("http", 8080)])]

    def initial_policy(self):
        """The policy as it stands before the incident: chat on the alternate
        gateway, and the bulkheads long since loosened (see the change log)."""
        return (
            "# Traffic manager policy; re-read every probe interval.\n"
            "probe = admission\n"
            "probe_interval = 5\n"
            "probe_timeout = 2\n"
            "unhealthy_threshold = 2\n"
            "healthy_threshold = 3\n"
            "max_ejection_percent = 100\n"
            "spillover = global\n"
            "spillover_below = 0.67\n"
            "anomaly_redirect = enabled\n"
            "anomaly_threshold = 0.3\n"
            "drained =\n"
            "placement.mail = primary\n"
            "placement.chat = alternate\n"
        )

    def transport_site(self):
        cluster = f"{self.namespace}.svc.cluster.local"
        return (
            f"myhostname = mx.{MAIL_DOMAIN}\n"
            f"virtual_mailbox_domains = {MAIL_DOMAIN}\n"
            "# Mailbox delivery goes through the region-a gateway.\n"
            f"virtual_transport = lmtp:inet:mail-a.{cluster}:1024\n"
            f"transport_maps = inline:{{ {PARTNER_DOMAIN}=smtp:[partners-mx.{cluster}]:2525 }}\n"
            "lmtp_host_lookup = native\n"
            "smtp_host_lookup = native\n"
            "mynetworks = 0.0.0.0/0\n"
            "lmtp_connect_timeout = 10s\n"
            "queue_run_delay = 60s\n"
            "minimal_backoff_time = 60s\n"
            "maximal_backoff_time = 120s\n"
            "# Tenant policy: undeliverable mail is returned to the sender after this.\n"
            f"maximal_queue_lifetime = {MAIL_QUEUE_LIFETIME}\n"
            f"bounce_queue_lifetime = {MAIL_QUEUE_LIFETIME}\n"
        )

    def application_documents(self):
        documents = super().application_documents()
        mailbox = self.mail_statefulset(
            "mailbox",
            "mailbox",
            [("imap", 143), ("lmtp", 24)],
            [
                {"name": "mail", "mountPath": "/var/mail"},
                {"name": "accounts", "mountPath": "/etc/mail-users", "readOnly": True},
            ],
            [{"name": "accounts", "secret": {"secretName": "mail-accounts"}}],
            "mail",
        )
        transport = self.mail_statefulset(
            "mail-transport",
            "transport",
            [("smtp", 25)],
            [
                {"name": "queue", "mountPath": "/var/spool/postfix"},
                {"name": "site", "mountPath": "/etc/postfix-site", "readOnly": True},
            ],
            [{"name": "site", "configMap": {"name": "mail-transport-site"}}],
            "queue",
        )
        alt = self.deployment(
            "edge-alt",
            {
                "name": "haproxy",
                "image": HAPROXY_IMAGE,
                "ports": [
                    {"name": "mail-imap", "containerPort": 1143},
                    {"name": "mail-lmtp", "containerPort": 1024},
                    {"name": "chat", "containerPort": 8065},
                    {"name": "metrics", "containerPort": 8404},
                ],
                "volumeMounts": [{"name": "config", "mountPath": "/usr/local/etc/haproxy"}],
                "readinessProbe": {"tcpSocket": {"port": 8404}, "periodSeconds": 5},
                "resources": {
                    "requests": {"cpu": "100m", "memory": "64Mi"},
                    "limits": {"cpu": "500m", "memory": "256Mi"},
                },
            },
            [{"name": "config", "configMap": {"name": "edge-alt"}}],
        )
        services = [
            self.plain_service("mailbox", [("imap", 143), ("lmtp", 24)]),
            self.plain_service("mail-transport", [("smtp", 25)]),
            self.plain_service("edge-alt", [("mail-imap", 1143), ("mail-lmtp", 1024), ("chat", 8065)]),
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "partners-mx"},
                "spec": {
                    "type": "ExternalName",
                    "externalName": f"{CLIENTS}.{HIDDEN_NAMESPACE}.svc.cluster.local",
                },
            },
            *[
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {"name": f"edge-gw-{r}"},
                    "spec": {
                        "clusterIP": "None",
                        "selector": {"app": "edge-gw", "region": r},
                        "ports": [{"port": 9100}],
                    },
                }
                for r in REGIONS
            ],
            *[self.steered_service(s, r) for s in ("mail", "chat") for r in REGIONS],
        ]
        configs = [
            self.config_map("edge-gw", {"edge-gw.conf": EDGE_CONFIG}),
            self.config_map("edge-alt", {"haproxy.cfg": HAPROXY_CONFIG}),
            self.config_map("traffic-policy", {"policy": self.initial_policy()}),
            self.config_map("mail-transport-site", {"site.cf": self.transport_site()}),
            self.config_map("operations-log", {"changes": self.change_log()}),
        ]
        return [
            *documents,
            *configs,
            *services,
            mailbox,
            transport,
            alt,
            *self.traffic_manager_documents(),
            *[self.gateway_statefulset(r) for r in REGIONS],
        ]

    def change_log(self, now=None):
        """Operational history up to deployment. The incident adds to it."""
        now = now or datetime.now(UTC)
        day = lambda days: (now - timedelta(days=days)).strftime("%Y-%m-%d")  # noqa: E731
        entries = [
            (day(171), "edge-gw", "Release 2026.06.4 to all regions."),
            (day(150), "mattermost", "Database maintenance window; vacuum and reindex."),
            (
                day(128),
                "edge-gw",
                "Release 2026.07.2 (connection handling update for gateway hosts), staged region by "
                "region over three weeks. No regressions.",
            ),
            (day(110), "mailbox", "Mailbox storage grown to 2Gi."),
            (day(97), "mailbox", "Raised Dovecot client limits; idle IMAP sessions hibernate."),
            (day(83), "edge-gw", "Release 2026.09.1: worker_threads and admission_timeout settings."),
            (
                day(64),
                "traffic-policy",
                "max_ejection_percent 50 -> 100 so units failing probes are never kept in rotation.",
            ),
            (day(52), "edge-gw", "Probe listener timeout aligned with traffic-manager probe_timeout."),
            (day(41), "edge-alt", "HAProxy maxconn sized for chat failover (1200 global)."),
            (day(23), "traffic-policy", "spillover regional -> global for the region-c network maintenance."),
            (day(16), "edge-gw", "Release 2026.11.1 to all regions."),
            (day(9), "mattermost", "Minor version update."),
            (day(3), "mail-transport", "Queue retry timers shortened (60s/120s)."),
        ]
        return "\n".join(f"{d}  {c:<15} {t}" for d, c, t in entries) + "\n"

    @property
    def expected_volume_count(self):
        return super().expected_volume_count + len(self.statefulset_volumes)

    # ---- lifecycle -----------------------------------------------------------------

    def accounts(self):
        """Mail accounts as user@domain:password lines, stable for the namespace."""
        existing = self.command(
            "get", "secret", "mail-accounts", "--ignore-not-found", "-o", "jsonpath={.data.users}"
        ).strip()
        if existing:
            return base64.b64decode(existing).decode()
        return "".join(
            f"user{i:04d}@{MAIL_DOMAIN}:{secrets.token_hex(8)}\n" for i in range(1, self.tier["accounts"] + 1)
        )

    def initialize(self):
        users = self.accounts()
        self.apply(
            [
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": "mail-accounts"},
                    "stringData": {"users": users},
                }
            ]
        )

    def roll_out_history(self):
        """Roll every gateway StatefulSet through the earlier releases, so its
        revision history is the fleet's real release history. Runs once."""
        if self.command("get", "statefulset", "-l", "app=edge-gw", "-o", "name").strip():
            return
        self.apply([self.config_map("edge-gw", {"edge-gw.conf": EDGE_CONFIG_2606})])
        for release in EDGE_RELEASES[:-1]:
            if release[0] == "2026.09.1":
                self.apply([self.config_map("edge-gw", {"edge-gw.conf": EDGE_CONFIG})])
            self.apply([self.gateway_statefulset(r, release) for r in REGIONS])
            for r in REGIONS:
                self.command("rollout", "status", f"statefulset/edge-gw-{r}", "--timeout=600s", timeout=630)

    def deploy(self):
        if not self.command("get", "namespace", self.namespace, "--ignore-not-found", "-o", "name").strip():
            self.create_namespace()
        self.initialize()
        self.roll_out_history()
        super().deploy()
        for name in self.incident_deployments:
            self.command("rollout", "status", f"deployment/{name}", "--timeout=600s", timeout=630)
        for name in ("mailbox", "mail-transport", *[f"edge-gw-{r}" for r in REGIONS]):
            self.command("rollout", "status", f"statefulset/{name}", "--timeout=600s", timeout=630)
        self.deploy_clients()

    def run_client(self, mode, **arguments):
        url = f"http://{self.slug}:{self.frontend_port}"
        components = Path(MattermostApplication.client_workflow).parent.parent / "components"
        return PythonClientRunner(self.command).run(
            [components / "http_client.py", MattermostApplication.client_workflow, self.client_workflow],
            f"print(json.dumps(workflow(Client({url!r}), {mode!r}, **json.loads({json.dumps(arguments)!r}))))",
            timeout=600,
        )

    def chat_tokens(self):
        result = self.run_client("chat-users", count=self.tier["chat_users"])
        return result["tokens"]

    def deploy_clients(self):
        """Start the hidden user population at the quiet baseline."""
        if not self.command("get", "namespace", HIDDEN_NAMESPACE, "--ignore-not-found", "-o", "name").strip():
            self.command("create", "namespace", HIDDEN_NAMESPACE)
        users = self.accounts()
        tokens = self.chat_tokens()
        population = ",".join(f"{s}.{r}={self.tier['baseline'][s]}" for s in ("mail", "chat") for r in REGIONS)
        labels = {"app": CLIENTS}
        documents = [
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": CLIENTS, "namespace": HIDDEN_NAMESPACE, "labels": labels},
                "stringData": {"users": users, "chat-tokens": "\n".join(tokens) + "\n"},
            },
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {"name": CLIENTS, "namespace": HIDDEN_NAMESPACE, "labels": labels},
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "storageClassName": self.storage_class,
                    "resources": {"requests": {"storage": "1Gi"}},
                },
            },
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": CLIENTS, "namespace": HIDDEN_NAMESPACE, "labels": labels},
                "spec": {
                    "replicas": 1,
                    "strategy": {"type": "Recreate"},
                    "selector": {"matchLabels": labels},
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": {
                            "automountServiceAccountToken": False,
                            "securityContext": {"fsGroup": 10001},
                            "containers": [
                                {
                                    "name": "clients",
                                    "image": EDGE_IMAGE,
                                    "command": ["/usr/local/bin/workspace-clients"],
                                    "args": [
                                        f"-mail-target=mail-%s.{self.namespace}.svc.cluster.local:1143",
                                        f"-chat-target=chat-%s.{self.namespace}.svc.cluster.local:8065",
                                        f"-smtp=mail-transport.{self.namespace}.svc.cluster.local:25",
                                        "-users=/etc/workspace/users",
                                        "-chat-tokens=/etc/workspace/chat-tokens",
                                        "-mail-rate=1",
                                        f"-population={population}",
                                    ],
                                    "ports": [{"containerPort": 8090}, {"containerPort": 2525}],
                                    "volumeMounts": [
                                        {"name": "config", "mountPath": "/etc/workspace", "readOnly": True},
                                        {"name": "state", "mountPath": "/data"},
                                    ],
                                    "resources": {
                                        "requests": {"cpu": "200m", "memory": "256Mi"},
                                        "limits": {"memory": "1Gi"},
                                    },
                                }
                            ],
                            "volumes": [
                                {"name": "config", "secret": {"secretName": CLIENTS}},
                                {"name": "state", "persistentVolumeClaim": {"claimName": CLIENTS}},
                            ],
                        },
                    },
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": CLIENTS, "namespace": HIDDEN_NAMESPACE, "labels": labels},
                "spec": {
                    "selector": labels,
                    "ports": [{"name": "control", "port": 8090}, {"name": "sink", "port": 2525}],
                },
            },
        ]
        # A later -n overrides the application namespace.
        self.hidden("apply", "-f", "-", input_text=yaml.safe_dump_all(documents, sort_keys=False))
        self.hidden("rollout", "status", f"deployment/{CLIENTS}", "--timeout=300s", timeout=330)

    # ---- operations the problem and oracle use ----------------------------------

    def hidden(self, *args, input_text=None, timeout=60):
        return self.command("-n", HIDDEN_NAMESPACE, *args, input_text=input_text, timeout=timeout)

    def clients_api(self, path, method="GET"):
        flags = ["-q", "-O-", "-T", "20"]
        if method == "POST":
            flags.append("--post-data=")
        output = self.hidden(
            "exec", f"deployment/{CLIENTS}", "--", "wget", *flags, f"http://127.0.0.1:8090{path}", timeout=40
        )
        return output

    def client_status(self, window="60s"):
        return json.loads(self.clients_api(f"/status?window={window}"))

    def set_population(self, counts):
        spec = ",".join(f"{k}={v}" for k, v in sorted(counts.items()))
        return json.loads(self.clients_api(f"/population?set={spec}", method="POST"))

    def population_for(self, level, factor=1.0):
        return {f"{s}.{r}": int(self.tier[level][s] * factor) for s in ("mail", "chat") for r in REGIONS}

    def traffic_status(self):
        return json.loads(
            self.command(
                "exec",
                "deployment/traffic-manager",
                "--",
                "python",
                "-c",
                "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/status').read().decode())",
            )
        )

    def policy(self):
        return traffic_manager.parse_policy(
            self.command("get", "configmap", "traffic-policy", "-o", "jsonpath={.data.policy}")
        )

    def set_policy(self, **changes):
        text = self.command("get", "configmap", "traffic-policy", "-o", "jsonpath={.data.policy}")
        lines, seen = [], set()
        for line in text.splitlines():
            key = line.split("=", 1)[0].strip()
            if key in changes:
                line = f"{key} = {changes[key]}"
                seen.add(key)
            lines.append(line)
        lines += [f"{k} = {v}" for k, v in changes.items() if k not in seen]
        patch = {"data": {"policy": "\n".join(lines) + "\n"}}
        self.command("patch", "configmap", "traffic-policy", "--type=merge", "-p", json.dumps(patch))

    def append_change(self, component, text):
        log = self.command("get", "configmap", "operations-log", "-o", "jsonpath={.data.changes}")
        stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
        patch = {"data": {"changes": log.rstrip("\n") + f"\n{stamp}  {component:<15} {text}\n"}}
        self.command("patch", "configmap", "operations-log", "--type=merge", "-p", json.dumps(patch))

    def transport(self, *args, timeout=60):
        return self.command("exec", "statefulset/mail-transport", "--", *args, timeout=timeout)

    def deferred_count(self):
        output = self.transport("postqueue", "-j")
        return sum(1 for line in output.splitlines() if line.strip())

    def delivered_message_ids(self):
        """Message-ID header of every stored message, with repeats."""
        output = self.command(
            "exec",
            "statefulset/mailbox",
            "--",
            "doveadm",
            "-f",
            "flow",
            "fetch",
            "-A",
            "hdr.message-id",
            "all",
            timeout=300,
        )
        ids = []
        for line in output.splitlines():
            _, _, value = line.partition("hdr.message-id=")
            if value.strip():
                ids.append(value.strip())
        return ids

    def accepted_mail(self):
        output = self.clients_api("/mail/accepted")
        return [line.split()[1] for line in output.splitlines() if len(line.split()) >= 2]

    def bounced_mail(self):
        output = self.clients_api("/mail/bounced")
        return [line.split()[1] for line in output.splitlines() if len(line.split()) >= 2]

    def unit_load_test(self, units, clients, seconds):
        """Hold `clients` long-lived IMAP connections on each unit at once,
        from the hidden namespace, and return each run's per-second report."""
        names = {}
        for unit in units:
            ip = self.command("get", "pod", unit, "-o", "jsonpath={.status.podIP}").strip()
            name = f"grading-load-{unit}"
            names[unit] = name
            self.hidden("delete", "pod", name, "--ignore-not-found", "--wait=true", timeout=120)
            self.hidden(
                "run",
                name,
                f"--image={EDGE_IMAGE}",
                "--restart=Never",
                "--labels=app=grading-load",
                "--command",
                "--",
                "/usr/local/bin/edge-load",
                f"-target={ip}:1143",
                f"-clients={clients}",
                "-hold=60s",
                "-retry=1s",
                "-timeout=5s",
                f"-duration={seconds}s",
            )
        reports = {}
        for unit, name in names.items():
            self.hidden(
                "wait",
                "--for=jsonpath={.status.phase}=Succeeded",
                f"pod/{name}",
                f"--timeout={seconds + 180}s",
                timeout=seconds + 200,
            )
            reports[unit] = self.hidden("logs", name, timeout=60)
            self.hidden("delete", "pod", name, "--ignore-not-found", timeout=120)
        return reports

    def start_workload(self):
        """The user population is part of the application; nothing to add."""

    def stop_workload(self):
        pass

    def cleanup(self):
        if self.command("get", "namespace", HIDDEN_NAMESPACE, "--ignore-not-found", "-o", "name").strip():
            self.hidden(
                "delete",
                "deployment,service,secret,pvc",
                "-l",
                f"app={CLIENTS}",
                "--ignore-not-found",
                "--wait=true",
                timeout=200,
            )
        super().cleanup()

    def wait_for(self, predicate, timeout, interval=5, what="condition"):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                last = predicate()
                if last:
                    return last
            except Exception as error:  # transient while pods restart
                last = error
            time.sleep(interval)
        raise TimeoutError(f"{what} not reached within {timeout}s (last: {last!r})")
