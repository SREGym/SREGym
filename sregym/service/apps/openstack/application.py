"""A small OpenStack region: real Keystone, Glance, Placement and Nova on Kubernetes.

Hypervisors are nova-compute processes running Nova's fake virt driver, one per
StatefulSet pod and grouped by rack, so a tier can model tens of hypervisors on
one machine while the scheduler, resource tracker, placement inventory, RPC and
service heartbeats are all the real ones. Tenants are separate projects whose
traffic runs from ``openstack-tenants``, outside the incident fault domain.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import time
from pathlib import Path

from sregym.service.apps.base import Application
from sregym.service.apps.components.client_runner import PythonClientRunner
from sregym.service.apps.components.kubernetes import KubernetesCommands
from sregym.service.apps.openstack import config, object_storage
from sregym.service.apps.openstack.tiers import TIERS, CloudTier
from sregym.service.kubectl import KubeCtl

KOLLA_TAG = "2025.1-ubuntu-noble"
RUNTIME = Path(__file__).with_name("runtime")
TENANT_NAMESPACE = "openstack-tenants"
MAINTENANCE_REASON = "HW-4471: correctable ECC errors on DIMM B2, awaiting replacement"
# Tenant operations wait at most this long, so a measurement window is complete
# once this much time has passed after it closes.
WORKLOAD_COMPLETION_TIMEOUT_S = 100
PASSWORD_KEYS = (
    "admin",
    "db_root",
    "db_keystone",
    "db_glance",
    "db_placement",
    "db_nova",
    "db_neutron",
    "nova",
    "neutron",
    "placement",
    "glance",
    "rabbit_admin",
    "rabbit_openstack",
    "swift",
    "swift_hash_prefix",
    "swift_hash_suffix",
)


def image(service: str) -> str:
    return f"quay.io/openstack.kolla/{service}:{KOLLA_TAG}"


# Every Nova service, the toolbox and the tenant workload share one image.
NOVA_IMAGE = image("nova-api")


def resources(cpu: str, memory: str, limit: str) -> dict:
    return {"requests": {"cpu": cpu, "memory": memory}, "limits": {"memory": limit}}


def rack_inventory_name(rack: str) -> str:
    return f"rack-{rack}-inventory"


class OpenStackCloud(KubernetesCommands, Application):
    app_id = "openstack"
    metadata_file = Path(__file__).with_name("metadata.json")
    tenant_namespace = TENANT_NAMESPACE

    def __init__(
        self,
        tier: str = "small",
        storage_class: str | None = None,
        object_storage: bool = False,
        object_replicas: int = 3,
    ):
        if tier not in TIERS:
            raise ValueError(f"Unknown OpenStack tier {tier!r}; choose from {tuple(TIERS)}")
        super().__init__(self.metadata_file)
        self.scale_tier = tier
        self.tier: CloudTier = TIERS[tier]
        self.storage_class = storage_class
        #: Deploy Swift (storage nodes, proxy, ring builder) and object traffic.
        self.object_storage = object_storage
        self.object_replicas = object_replicas
        self.load_app_json()
        self.kubectl = KubeCtl()
        self.helm_deploy = False
        self.endpoints = config.Endpoints(self.namespace)
        self.passwords: dict[str, str] = {}
        self.resource_ids: dict[str, str] = {}

    def load_app_json(self):
        super().load_app_json()
        metadata = self.get_app_json()
        self.app_name = metadata["Name"]
        self.description = metadata["Desc"]

    def get_app_json(self):
        metadata = super().get_app_json()
        if hasattr(self, "tier"):
            metadata["Desc"] += (
                f" Tier {self.tier.name}: {len(self.tier.hypervisors)} hypervisors in racks "
                f"{', '.join(self.tier.racks)}; {self.tier.tenants} tenant projects."
            )
        if getattr(self, "object_storage", False):
            metadata["Desc"] += (
                " Swift object storage: the swift-proxy deployment, the swift-storage StatefulSet (one storage"
                " node per rack, each its own zone) and the swift-ring-builder deployment, which"
                " holds the ring builder files in /etc/swift-builders (with backups/, changes.log and RUNBOOK.md)"
                " and serves the rings every Swift pod installs."
            )
        return metadata

    # ------------------------------------------------------------------ commands
    def tenant_command(self, *args, input_text=None, timeout=120):
        return subprocess.run(
            ["kubectl", "-n", self.tenant_namespace, *args],
            input=input_text,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        ).stdout

    def toolbox(self, *args, timeout=180) -> str:
        return self.command("exec", "deploy/ops-toolbox", "--", *args, timeout=timeout)

    def openstack(self, *args, timeout=120, attempts=3):
        """Run the admin ``openstack`` CLI as the grader and return its parsed JSON output.

        It runs in the region verifier, outside the incident namespace, so the
        agent's changes to the ops toolbox cannot alter what grading sees.
        Transient API errors (a restarting Keystone) are retried.
        """
        for attempt in range(attempts):
            try:
                output = self.tenant_command(
                    "exec", "deploy/region-verifier", "--", "openstack", *args, "-f", "json", timeout=timeout
                )
                return json.loads(output or "null")
            except subprocess.CalledProcessError:
                if attempt == attempts - 1:
                    raise
                time.sleep(5 * (attempt + 1))

    def sql(self, statement: str, database: str = "") -> str:
        args = ["exec", "mariadb-0", "--", "mysql", "-uroot", "-N", "-B"]
        if database:
            args.append(database)
        return self.command(*args, "-e", statement).strip()

    def rabbitmqctl(self, *args) -> str:
        return self.command("exec", "rabbitmq-0", "--", "rabbitmqctl", *args)

    def run_bootstrap(self, mode: str, **arguments):
        return PythonClientRunner(self.command, pod=self.toolbox_pod()).run(
            [RUNTIME / "bootstrap.py"], f"run({mode!r}, **json.loads({json.dumps(arguments)!r}))", timeout=600
        )

    def toolbox_pod(self) -> str:
        return self.command(
            "get",
            "pods",
            "-l",
            "component=ops-toolbox",
            "--field-selector=status.phase=Running",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ).strip()

    # ------------------------------------------------------------------ secrets
    def load_passwords(self) -> dict[str, str]:
        existing = self.command("get", "secret", "openstack-passwords", "--ignore-not-found", "-o", "json").strip()
        data = json.loads(existing)["data"] if existing else {}
        stored = {key: base64.b64decode(value).decode() for key, value in data.items()}
        # Keep every stored password; add any a newer version of the region needs.
        self.passwords = {key: stored.get(key) or secrets.token_hex(16) for key in PASSWORD_KEYS} | stored
        return self.passwords

    def tenant_accounts(self) -> list[dict]:
        return [
            {
                "project": f"tenant-{index:02d}",
                "user": f"tenant-{index:02d}-ops",
                "password": self.passwords["admin"][:8] + f"-tenant-{index:02d}",
            }
            for index in range(1, self.tier.tenants + 1)
        ]

    def broker_host_key(self) -> bytes:
        secret = json.loads(self.command("get", "secret", "hypervisor-broker-key", "-o", "json"))
        return base64.b64decode(secret["data"]["host-key"]).strip()

    # ------------------------------------------------------------------ render
    def render_secrets(self) -> list[dict]:
        passwords = self.passwords
        endpoints = self.endpoints
        fernet = {str(i): base64.urlsafe_b64encode(os.urandom(32)).decode() for i in range(2)}
        credential = {str(i): base64.urlsafe_b64encode(os.urandom(32)).decode() for i in range(2)}
        files = {
            "keystone.conf": config.keystone_conf(endpoints, passwords),
            "placement.conf": config.placement_conf(endpoints, passwords),
            "glance-api.conf": config.glance_conf(endpoints, passwords),
            "nova.conf": config.nova_conf(endpoints, passwords),
            "neutron.conf": config.neutron_conf(endpoints, passwords),
            "rabbitmq.conf": config.rabbitmq_conf(),
            "definitions.json": config.rabbitmq_definitions(passwords, os.urandom(4)),
            "enabled_plugins": "[rabbitmq_management].\n",
            "clouds.yaml": config.clouds_yaml(endpoints, passwords["admin"]),
        }

        def secret(name, data, keep=False):
            document = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name}, "stringData": data}
            return (name, document, keep)

        return [
            # Merged with what was stored, so re-applying never changes a password.
            secret("openstack-passwords", passwords),
            secret("openstack-config", files),
            secret("keystone-fernet-keys", fernet, keep=True),
            secret("keystone-credential-keys", credential, keep=True),
            secret("hypervisor-broker-key", {"host-key": secrets.token_hex(32)}, keep=True),
            secret(
                "broker-admin",
                {"username": "admin", "password": passwords["rabbit_admin"]},
            ),
        ]

    def runtime_configmap(self) -> dict:
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "openstack-runtime"},
            "data": {
                name: (RUNTIME / name).read_text()
                for name in (
                    "hypervisor.py",
                    "host_access_reconciler.py",
                    "bootstrap.py",
                    "neutron_server.py",
                    "ring_sync.py",
                    "swift_storage.sh",
                    "swift_rings.py",
                )
            },
        }

    def inventory_document(self, rack: str) -> dict:
        hosts = [h for h in self.tier.hypervisors if self.tier.rack_of(h) == rack]
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": rack_inventory_name(rack), "labels": {"inventory.openstack/rack": rack}},
            "data": {
                "inventory.json": json.dumps(
                    {
                        "rack": rack,
                        "source": "dcim-export",
                        "hosts": [{"name": h, "role": "compute"} for h in hosts],
                    },
                    indent=2,
                )
            },
        }

    def config_volume(self, *items: tuple[str, str]) -> dict:
        return {
            "name": "config",
            "secret": {"secretName": "openstack-config", "items": [{"key": k, "path": p} for k, p in items]},
        }

    @staticmethod
    def labels(component: str) -> dict:
        return {"app.kubernetes.io/part-of": "openstack", "component": component}

    def service(self, name: str, component: str, ports: list[int], headless: bool = False) -> dict:
        spec = {"selector": self.labels(component), "ports": [{"name": f"p{p}", "port": p} for p in ports]}
        if headless:
            spec["clusterIP"] = "None"
        return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name}, "spec": spec}

    def deployment(self, name: str, pod_spec: dict, replicas: int = 1) -> dict:
        labels = self.labels(name)
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "labels": labels},
            "spec": {
                "replicas": replicas,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {"automountServiceAccountToken": False, "enableServiceLinks": False, **pod_spec},
                },
            },
        }

    def render_infrastructure(self) -> list[dict]:
        claim = {
            "metadata": {"name": "data"},
            "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "4Gi"}}},
        }
        if self.storage_class:
            claim["spec"]["storageClassName"] = self.storage_class
        mariadb_start = (
            "set -e; mkdir -p /run/mysqld && chown mysql:mysql /run/mysqld; "
            "if [ ! -d /var/lib/mysql/mysql ]; then "
            "mariadb-install-db --user=mysql --datadir=/var/lib/mysql --skip-test-db >/dev/null; fi; "
            "exec mariadbd --user=mysql --datadir=/var/lib/mysql --bind-address=0.0.0.0 --port=3306 "
            "--skip-name-resolve --max-connections=500 --innodb-buffer-pool-size=128M "
            "--character-set-server=utf8mb4 --collation-server=utf8mb4_general_ci"
        )
        return [
            self.service("mariadb", "mariadb", [3306]),
            {
                "apiVersion": "apps/v1",
                "kind": "StatefulSet",
                "metadata": {"name": "mariadb", "labels": self.labels("mariadb")},
                "spec": {
                    "serviceName": "mariadb",
                    "replicas": 1,
                    "selector": {"matchLabels": self.labels("mariadb")},
                    "template": {
                        "metadata": {"labels": self.labels("mariadb")},
                        "spec": {
                            "automountServiceAccountToken": False,
                            "containers": [
                                {
                                    "name": "mariadb",
                                    "image": image("mariadb-server"),
                                    "command": ["sh", "-c", mariadb_start],
                                    # Prepares its data directory, then drops to the mysql user.
                                    "securityContext": {"runAsUser": 0},
                                    "ports": [{"containerPort": 3306}],
                                    "volumeMounts": [{"name": "data", "mountPath": "/var/lib/mysql"}],
                                    "readinessProbe": {
                                        "exec": {"command": ["mysqladmin", "-uroot", "ping"]},
                                        "periodSeconds": 5,
                                    },
                                    "resources": resources("100m", "256Mi", "1Gi"),
                                }
                            ],
                        },
                    },
                    "volumeClaimTemplates": [claim],
                },
            },
            self.service("rabbitmq", "rabbitmq", [5672, 15672]),
            {
                "apiVersion": "apps/v1",
                "kind": "StatefulSet",
                "metadata": {"name": "rabbitmq", "labels": self.labels("rabbitmq")},
                "spec": {
                    "serviceName": "rabbitmq",
                    "replicas": 1,
                    "selector": {"matchLabels": self.labels("rabbitmq")},
                    "template": {
                        "metadata": {"labels": self.labels("rabbitmq")},
                        "spec": {
                            "automountServiceAccountToken": False,
                            "volumes": [
                                self.config_volume(
                                    ("rabbitmq.conf", "rabbitmq.conf"),
                                    ("enabled_plugins", "enabled_plugins"),
                                    ("definitions.json", "definitions.json"),
                                ),
                                {"name": "data", "emptyDir": {}},
                            ],
                            "containers": [
                                {
                                    "name": "rabbitmq",
                                    "image": image("rabbitmq"),
                                    "command": [
                                        "sh",
                                        "-c",
                                        "cp /config/* /etc/rabbitmq/ && chown -R rabbitmq: /var/lib/rabbitmq "
                                        "&& exec rabbitmq-server",
                                    ],
                                    "env": [{"name": "RABBITMQ_NODENAME", "value": "rabbit@localhost"}],
                                    # Installs its configuration; rabbitmq-server drops to its own user.
                                    "securityContext": {"runAsUser": 0},
                                    "ports": [{"containerPort": 5672}, {"containerPort": 15672}],
                                    "volumeMounts": [
                                        {"name": "config", "mountPath": "/config"},
                                        {"name": "data", "mountPath": "/var/lib/rabbitmq"},
                                    ],
                                    "readinessProbe": {
                                        "exec": {"command": ["rabbitmq-diagnostics", "-q", "check_port_connectivity"]},
                                        "periodSeconds": 10,
                                        "timeoutSeconds": 10,
                                    },
                                    "resources": resources("100m", "256Mi", "1Gi"),
                                }
                            ],
                        },
                    },
                },
            },
            self.service("memcached", "memcached", [11211]),
            self.deployment(
                "memcached",
                {
                    "containers": [
                        {
                            "name": "memcached",
                            "image": image("memcached"),
                            "command": ["memcached", "-u", "memcache", "-m", "128", "-p", "11211", "-l", "0.0.0.0"],
                            "ports": [{"containerPort": 11211}],
                            "resources": resources("20m", "32Mi", "256Mi"),
                        }
                    ]
                },
            ),
        ]

    def uwsgi(self, port: int, module: str, processes: int = 2) -> list[str]:
        return [
            "uwsgi",
            "--http-socket",
            f"0.0.0.0:{port}",
            "--module",
            module,
            "--master",
            "--processes",
            str(processes),
            "--threads",
            "1",
            "--enable-threads",
            "--lazy-apps",
            "--die-on-term",
            "--buffer-size",
            "65535",
            "--disable-logging",
        ]

    def api_container(self, name, service_image, command, port, mounts, probe_path="/", resources_=None):
        return {
            "name": name,
            "image": service_image,
            "command": command,
            "ports": [{"containerPort": port}],
            "volumeMounts": mounts,
            "readinessProbe": {"httpGet": {"path": probe_path, "port": port}, "periodSeconds": 5, "timeoutSeconds": 5},
            "resources": resources_ or resources("50m", "192Mi", "768Mi"),
        }

    def render_keystone(self) -> list[dict]:
        bootstrap = (
            "keystone-manage db_sync && keystone-manage bootstrap "
            f'--bootstrap-password "$ADMIN_PASSWORD" --bootstrap-admin-url {self.endpoints.keystone}/v3 '
            f"--bootstrap-internal-url {self.endpoints.keystone}/v3 --bootstrap-public-url {self.endpoints.keystone}/v3 "
            f"--bootstrap-region-id {config.REGION}"
        )
        mounts = [
            {"name": "config", "mountPath": "/etc/keystone/keystone.conf", "subPath": "keystone.conf"},
            {"name": "fernet", "mountPath": "/etc/keystone/fernet-keys"},
            {"name": "credential", "mountPath": "/etc/keystone/credential-keys"},
        ]
        return [
            self.service("keystone", "keystone", [5000]),
            self.deployment(
                "keystone",
                {
                    "volumes": [
                        self.config_volume(("keystone.conf", "keystone.conf")),
                        {"name": "fernet", "secret": {"secretName": "keystone-fernet-keys"}},
                        {"name": "credential", "secret": {"secretName": "keystone-credential-keys"}},
                    ],
                    "initContainers": [
                        {
                            "name": "bootstrap",
                            "image": image("keystone"),
                            "command": ["sh", "-c", bootstrap],
                            "env": [
                                {
                                    "name": "ADMIN_PASSWORD",
                                    "valueFrom": {"secretKeyRef": {"name": "openstack-passwords", "key": "admin"}},
                                }
                            ],
                            "volumeMounts": mounts,
                        }
                    ],
                    "containers": [
                        self.api_container(
                            "keystone",
                            image("keystone"),
                            self.uwsgi(5000, "keystone.wsgi.api:application", processes=3),
                            5000,
                            mounts,
                            probe_path="/v3",
                        )
                    ],
                },
            ),
        ]

    def render_services(self) -> list[dict]:
        nova_mounts = [{"name": "config", "mountPath": "/etc/nova/nova.conf", "subPath": "nova.conf"}]
        nova_volumes = [self.config_volume(("nova.conf", "nova.conf"))]
        placement_mounts = [
            {"name": "config", "mountPath": "/etc/placement/placement.conf", "subPath": "placement.conf"}
        ]
        neutron_mounts = [{"name": "config", "mountPath": "/etc/neutron/neutron.conf", "subPath": "neutron.conf"}]
        glance_mounts = [
            {"name": "config", "mountPath": "/etc/glance/glance-api.conf", "subPath": "glance-api.conf"},
            {"name": "images", "mountPath": "/var/lib/glance"},
        ]
        return [
            self.service("placement", "placement", [8778]),
            self.deployment(
                "placement",
                {
                    "volumes": [self.config_volume(("placement.conf", "placement.conf"))],
                    "initContainers": [
                        {
                            "name": "db-sync",
                            "image": image("placement-api"),
                            "command": ["placement-manage", "db", "sync"],
                            "volumeMounts": placement_mounts,
                        }
                    ],
                    "containers": [
                        self.api_container(
                            "placement",
                            image("placement-api"),
                            self.uwsgi(8778, "placement.wsgi.api:application"),
                            8778,
                            placement_mounts,
                        )
                    ],
                },
            ),
            self.service("glance", "glance", [9292]),
            self.deployment(
                "glance",
                {
                    "volumes": [
                        self.config_volume(("glance-api.conf", "glance-api.conf")),
                        {"name": "images", "emptyDir": {}},
                    ],
                    "initContainers": [
                        {
                            "name": "db-sync",
                            "image": image("glance-api"),
                            "command": ["glance-manage", "db_sync"],
                            "volumeMounts": glance_mounts,
                        }
                    ],
                    "containers": [
                        self.api_container(
                            "glance",
                            image("glance-api"),
                            ["glance-api", "--config-file", "/etc/glance/glance-api.conf"],
                            9292,
                            glance_mounts,
                            probe_path="/versions",
                        )
                    ],
                },
            ),
            self.service("neutron", "neutron", [9696]),
            self.deployment(
                "neutron",
                {
                    "volumes": [
                        self.config_volume(("neutron.conf", "neutron.conf")),
                        {"name": "runtime", "configMap": {"name": "openstack-runtime"}},
                    ],
                    "initContainers": [
                        {
                            "name": "db-sync",
                            "image": image("neutron-server"),
                            "command": [
                                "neutron-db-manage",
                                "--config-file",
                                "/etc/neutron/neutron.conf",
                                "upgrade",
                                "heads",
                            ],
                            "volumeMounts": neutron_mounts,
                        }
                    ],
                    "containers": [
                        self.api_container(
                            "neutron",
                            image("neutron-server"),
                            ["python", "/runtime/neutron_server.py", "--config-file", "/etc/neutron/neutron.conf"],
                            9696,
                            [*neutron_mounts, {"name": "runtime", "mountPath": "/runtime"}],
                        )
                    ],
                },
            ),
            self.service("nova-api", "nova-api", [8774]),
            self.deployment(
                "nova-api",
                {
                    "volumes": nova_volumes,
                    "containers": [
                        self.api_container(
                            "nova-api",
                            NOVA_IMAGE,
                            self.uwsgi(8774, "nova.wsgi.osapi_compute:application"),
                            8774,
                            nova_mounts,
                        )
                    ],
                },
            ),
            *[
                self.deployment(
                    name,
                    {
                        "volumes": nova_volumes,
                        "containers": [
                            {
                                "name": name,
                                "image": NOVA_IMAGE,
                                "command": [name, "--config-file", "/etc/nova/nova.conf"],
                                "volumeMounts": nova_mounts,
                                "resources": resources("50m", "192Mi", "768Mi"),
                            }
                        ],
                    },
                )
                for name in ("nova-conductor", "nova-scheduler")
            ],
        ]

    def render_toolbox(self) -> list[dict]:
        return [
            self.deployment(
                "ops-toolbox",
                {
                    "volumes": [
                        self.config_volume(("nova.conf", "nova.conf"), ("clouds.yaml", "clouds.yaml")),
                    ],
                    "containers": [
                        {
                            "name": "toolbox",
                            "image": NOVA_IMAGE,
                            "command": ["sleep", "infinity"],
                            "env": [{"name": "OS_CLOUD", "value": "admin"}],
                            "volumeMounts": [
                                {"name": "config", "mountPath": "/etc/nova/nova.conf", "subPath": "nova.conf"},
                                {"name": "config", "mountPath": "/etc/openstack/clouds.yaml", "subPath": "clouds.yaml"},
                            ],
                            "resources": resources("10m", "64Mi", "512Mi"),
                        }
                    ],
                },
            )
        ]

    def broker_env(self) -> list[dict]:
        return [
            {"name": "BROKER_API", "value": f"http://{self.endpoints.host('rabbitmq')}:15672/api"},
            {"name": "BROKER_ADMIN_USER", "valueFrom": {"secretKeyRef": {"name": "broker-admin", "key": "username"}}},
            {
                "name": "BROKER_ADMIN_PASSWORD",
                "valueFrom": {"secretKeyRef": {"name": "broker-admin", "key": "password"}},
            },
        ]

    def reconciler_pod_spec(self) -> dict:
        return {
            "automountServiceAccountToken": False,
            "restartPolicy": "Never",
            "volumes": [
                {"name": "runtime", "configMap": {"name": "openstack-runtime"}},
                {"name": "host-key", "secret": {"secretName": "hypervisor-broker-key"}},
                {
                    "name": "inventory",
                    "projected": {
                        "sources": [
                            {
                                "configMap": {
                                    "name": rack_inventory_name(rack),
                                    # Racks are added and retired over time; a rack
                                    # without a published inventory is skipped.
                                    "optional": True,
                                    "items": [{"key": "inventory.json", "path": f"{rack}.json"}],
                                }
                            }
                            for rack in self.tier.racks
                        ]
                    },
                },
            ],
            "containers": [
                {
                    "name": "reconcile",
                    "image": NOVA_IMAGE,
                    "command": ["python", "/runtime/host_access_reconciler.py"],
                    "env": self.broker_env(),
                    "volumeMounts": [
                        {"name": "runtime", "mountPath": "/runtime"},
                        {"name": "host-key", "mountPath": "/etc/broker"},
                        {"name": "inventory", "mountPath": "/inventory"},
                    ],
                    "resources": resources("10m", "32Mi", "256Mi"),
                }
            ],
        }

    def render_host_access(self) -> list[dict]:
        return [
            *[self.inventory_document(rack) for rack in self.tier.racks],
            {
                "apiVersion": "batch/v1",
                "kind": "CronJob",
                "metadata": {"name": "host-access-reconciler", "labels": self.labels("host-access-reconciler")},
                "spec": {
                    "schedule": "* * * * *",
                    "concurrencyPolicy": "Forbid",
                    "successfulJobsHistoryLimit": 3,
                    "failedJobsHistoryLimit": 3,
                    "jobTemplate": {
                        "spec": {
                            "backoffLimit": 0,
                            "ttlSecondsAfterFinished": 600,
                            "template": {
                                "metadata": {"labels": self.labels("host-access-reconciler")},
                                "spec": self.reconciler_pod_spec(),
                            },
                        }
                    },
                },
            },
        ]

    def render_hypervisors(self) -> list[dict]:
        documents = [self.service("hypervisors", "hypervisor", [8080], headless=True)]
        for rack in self.tier.racks:
            labels = {**self.labels("hypervisor"), "rack": rack}
            documents.append(
                {
                    "apiVersion": "apps/v1",
                    "kind": "StatefulSet",
                    "metadata": {"name": f"hv-{rack}", "labels": labels},
                    "spec": {
                        "serviceName": "hypervisors",
                        "replicas": self.tier.hosts_per_rack,
                        "podManagementPolicy": "Parallel",
                        "selector": {"matchLabels": labels},
                        "template": {
                            "metadata": {"labels": labels},
                            "spec": {
                                "automountServiceAccountToken": False,
                                "enableServiceLinks": False,
                                "volumes": [
                                    self.config_volume(("nova.conf", "nova.conf")),
                                    {"name": "runtime", "configMap": {"name": "openstack-runtime"}},
                                    {"name": "host-key", "secret": {"secretName": "hypervisor-broker-key"}},
                                ],
                                "containers": [
                                    {
                                        "name": "nova-compute",
                                        "image": NOVA_IMAGE,
                                        "command": ["python", "/runtime/hypervisor.py"],
                                        "env": [
                                            {"name": "BROKER_HOST", "value": self.endpoints.host("rabbitmq")},
                                            {
                                                "name": "POD_IP",
                                                "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}},
                                            },
                                        ],
                                        "volumeMounts": [
                                            {
                                                "name": "config",
                                                "mountPath": "/etc/nova/nova.conf",
                                                "subPath": "nova.conf",
                                            },
                                            {"name": "runtime", "mountPath": "/runtime"},
                                            {"name": "host-key", "mountPath": "/etc/broker"},
                                        ],
                                        "resources": resources("20m", "160Mi", "512Mi"),
                                    }
                                ],
                            },
                        },
                    },
                }
            )
        return documents

    def render_tenants(self) -> list[dict]:
        # app=load-generator keeps the tenants and the verifier out of the agent's view.
        hidden = {"app": "load-generator", "app.kubernetes.io/part-of": "openstack-tenants"}
        labels = {**hidden, "component": "tenant-workload"}
        verifier = {**hidden, "component": "region-verifier"}
        accounts = json.dumps(self.tenant_accounts())
        container = {
            "image": NOVA_IMAGE,
            "env": [
                {"name": "OS_AUTH_URL", "value": f"{self.endpoints.keystone}/v3"},
                {"name": "THINK_TIME_S", "value": str(self.tier.think_time_s)},
                {"name": "WORKLOAD_SEED", "value": self.scale_tier},
                {"name": "LEDGER_PATH", "value": "/ledger/ledger.jsonl"},
                {"name": "OBJECT_STORAGE", "value": "1" if self.object_storage else "0"},
                {"name": "CANARIES_PER_TENANT", "value": str(self.tier.canaries_per_tenant)},
            ],
            "volumeMounts": [
                {"name": "workload", "mountPath": "/workload"},
                {"name": "accounts", "mountPath": "/etc/tenants"},
                {"name": "ledger", "mountPath": "/ledger"},
            ],
            "resources": resources("50m", "96Mi", "512Mi"),
        }
        volumes = [
            {"name": "workload", "configMap": {"name": "tenant-workload"}},
            {"name": "accounts", "secret": {"secretName": "tenant-accounts"}},
            {"name": "ledger", "emptyDir": {}},
        ]
        return [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "tenant-workload", "labels": labels},
                "data": {name: (RUNTIME / name).read_text() for name in ("tenant_workload.py", "object_audit.py")},
            },
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "tenant-accounts", "labels": labels},
                "stringData": {"accounts.json": accounts},
            },
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "tenant-seed", "labels": labels},
                "spec": {
                    "backoffLimit": 2,
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": {
                            "restartPolicy": "Never",
                            "automountServiceAccountToken": False,
                            "volumes": volumes,
                            "containers": [
                                {
                                    **container,
                                    "name": "seed",
                                    "command": [
                                        "python",
                                        "/workload/tenant_workload.py",
                                        "seed",
                                        str(self.tier.pets_per_tenant),
                                    ],
                                }
                            ],
                        },
                    },
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "region-verifier", "labels": verifier},
                "stringData": {"clouds.yaml": config.clouds_yaml(self.endpoints, self.passwords["admin"])},
            },
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "region-verifier", "labels": verifier},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": verifier},
                    "template": {
                        "metadata": {"labels": verifier},
                        "spec": {
                            "automountServiceAccountToken": False,
                            "volumes": [{"name": "clouds", "secret": {"secretName": "region-verifier"}}, *volumes],
                            "containers": [
                                {
                                    "name": "verifier",
                                    "image": NOVA_IMAGE,
                                    "command": ["sleep", "infinity"],
                                    "env": [{"name": "OS_CLOUD", "value": "admin"}],
                                    "volumeMounts": [{"name": "clouds", "mountPath": "/etc/openstack"}],
                                    "resources": resources("10m", "64Mi", "512Mi"),
                                },
                                *(
                                    [
                                        {
                                            "name": "object-verifier",
                                            "image": object_storage.SWIFT_IMAGE,
                                            "command": ["sleep", "infinity"],
                                            "env": [
                                                {"name": "OS_AUTH_URL", "value": f"{self.endpoints.keystone}/v3"},
                                                {
                                                    "name": "RING_SOURCE",
                                                    "value": f"http://{self.endpoints.host('swift-rings')}:8000",
                                                },
                                            ],
                                            "volumeMounts": container["volumeMounts"],
                                            "resources": resources("10m", "64Mi", "512Mi"),
                                        }
                                    ]
                                    if self.object_storage
                                    else []
                                ),
                            ],
                        },
                    },
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "tenant-workload", "labels": labels},
                "spec": {"selector": labels, "ports": [{"name": "http", "port": 8080}]},
            },
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "tenant-workload", "labels": labels},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": labels},
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": {
                            "automountServiceAccountToken": False,
                            "volumes": volumes,
                            "containers": [
                                {
                                    **container,
                                    "name": "tenants",
                                    "command": ["python", "/workload/tenant_workload.py"],
                                    "ports": [{"containerPort": 8080}],
                                    "readinessProbe": {
                                        "httpGet": {"path": "/status", "port": 8080},
                                        "periodSeconds": 5,
                                    },
                                }
                            ],
                        },
                    },
                },
            },
        ]

    # ------------------------------------------------------------------ deploy
    def apply_secrets(self):
        for name, document, keep in self.render_secrets():
            if keep and self.command("get", "secret", name, "--ignore-not-found", "-o", "name").strip():
                continue
            self.apply([document])

    def rollout(self, kind_name: str, timeout: int = 600):
        self.command("rollout", "status", kind_name, f"--timeout={timeout}s", timeout=timeout + 30)

    def bootstrap_databases(self):
        deadline = time.monotonic() + 300
        while True:
            try:
                self.sql("SELECT 1")
                break
            except subprocess.CalledProcessError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(3)
        p = self.passwords
        statements = []
        for database, user, password in (
            ("keystone", "keystone", p["db_keystone"]),
            ("glance", "glance", p["db_glance"]),
            ("placement", "placement", p["db_placement"]),
            ("nova_api", "nova", p["db_nova"]),
            ("nova", "nova", p["db_nova"]),
            ("nova_cell0", "nova", p["db_nova"]),
            ("neutron", "neutron", p["db_neutron"]),
        ):
            statements += [
                f"CREATE DATABASE IF NOT EXISTS {database};",
                f"CREATE USER IF NOT EXISTS '{user}'@'%' IDENTIFIED BY '{password}';",
                f"GRANT ALL PRIVILEGES ON {database}.* TO '{user}'@'%';",
            ]
        self.sql(" ".join(statements + ["FLUSH PRIVILEGES;"]))

    def bootstrap_nova(self):
        cell0 = self.endpoints.database("nova", self.passwords["db_nova"], "nova_cell0")
        self.toolbox("nova-manage", "api_db", "sync", timeout=600)
        self.toolbox("nova-manage", "cell_v2", "map_cell0", "--database_connection", cell0)
        if "cell1" not in self.toolbox("nova-manage", "cell_v2", "list_cells"):
            self.toolbox("nova-manage", "cell_v2", "create_cell", "--name=cell1", "--verbose")
        self.toolbox("nova-manage", "db", "sync", timeout=600)

    def run_reconciler(self, timeout: int = 180) -> str:
        """Run the scheduled host-access reconciler now and return its log."""
        name = f"host-access-reconciler-manual-{int(time.time())}"
        self.command("create", "job", name, "--from=cronjob/host-access-reconciler")
        self.command("wait", "--for=condition=complete", f"job/{name}", f"--timeout={timeout}s", timeout=timeout + 20)
        return self.command("logs", f"job/{name}")

    def compute_services(self, long: bool = False) -> list[dict]:
        # --long adds Disabled Reason and Forced Down.
        return self.openstack("compute", "service", "list", "--service", "nova-compute", *(["--long"] if long else []))

    def wait_hypervisors(self, hosts=None, state: str = "up", timeout: int = 600):
        expected = set(hosts or self.tier.hypervisors)
        deadline = time.monotonic() + timeout
        while True:
            try:
                services = json.loads(
                    self.toolbox("openstack", "compute", "service", "list", "--service", "nova-compute", "-f", "json")
                )
            except subprocess.CalledProcessError:
                services = []
            matching = {s["Host"] for s in services if s["State"] == state}
            if expected <= matching:
                return
            if time.monotonic() > deadline:
                raise RuntimeError(f"Hypervisors did not report {state}: {sorted(expected - matching)}")
            time.sleep(5)

    def deploy(self):
        self.create_namespace()
        self.load_passwords()
        self.apply_secrets()
        self.apply([self.runtime_configmap()])
        self.apply(self.render_infrastructure())
        for name in ("statefulset/mariadb", "statefulset/rabbitmq", "deployment/memcached"):
            self.rollout(name)
        self.bootstrap_databases()
        self.apply(self.render_keystone())
        self.rollout("deployment/keystone")
        self.apply(self.render_toolbox())
        self.rollout("deployment/ops-toolbox")
        endpoints = {
            "nova": self.endpoints.nova,
            "placement": self.endpoints.placement,
            "glance": self.endpoints.glance,
            "neutron": self.endpoints.neutron,
        }
        if self.object_storage:
            endpoints["swift"] = self.endpoints.swift
        self.run_bootstrap("catalog", endpoints=endpoints, passwords=self.passwords, accounts=[])
        self.bootstrap_nova()
        self.apply(self.render_services())
        for name in ("placement", "glance", "neutron", "nova-api", "nova-conductor", "nova-scheduler"):
            self.rollout(f"deployment/{name}")
        if self.object_storage:
            object_storage.deploy(self)
        self.apply(self.render_host_access())
        self.run_reconciler()
        self.apply(self.render_hypervisors())
        for rack in self.tier.racks:
            self.rollout(f"statefulset/hv-{rack}", timeout=900)
        self.wait_hypervisors()
        self.toolbox("nova-manage", "cell_v2", "discover_hosts", "--verbose")
        self.resource_ids = self.run_bootstrap("provision", accounts=self.tenant_accounts())
        self.deploy_tenants()
        for host in self.tier.maintenance_hosts:
            self.toolbox(
                "openstack",
                "compute",
                "service",
                "set",
                "--disable",
                "--disable-reason",
                MAINTENANCE_REASON,
                host,
                "nova-compute",
            )

    def deploy_tenants(self):
        if not self.tenant_command(
            "get", "namespace", self.tenant_namespace, "--ignore-not-found", "-o", "name"
        ).strip():
            self.tenant_command("create", "namespace", self.tenant_namespace)
        documents = self.render_tenants()
        manifest = [d for d in documents if d["kind"] != "Deployment"]
        self.tenant_command("apply", "-f", "-", input_text=_dump(manifest))
        self.tenant_command("wait", "--for=condition=complete", "job/tenant-seed", "--timeout=900s", timeout=920)
        self.tenant_command("apply", "-f", "-", input_text=_dump([d for d in documents if d["kind"] == "Deployment"]))
        for name in ("tenant-workload", "region-verifier"):
            self.tenant_command("rollout", "status", f"deployment/{name}", "--timeout=300s", timeout=320)

    # ------------------------------------------------------------------ state
    def pets(self) -> list[dict]:
        """Every tenant pet server, with its host, as the admin sees it."""
        servers = self.openstack("server", "list", "--all-projects", "--long", "--name", "^pet-", timeout=180)
        return sorted(
            (
                {
                    "id": s["ID"],
                    "name": s["Name"],
                    "status": s["Status"],
                    "task_state": s.get("Task State"),
                    "host": s.get("Host"),
                }
                for s in servers
            ),
            key=lambda s: s["name"],
        )

    def ledger(self, since: float, until: float | None = None) -> dict:
        path = (
            f"/api/v1/namespaces/{self.tenant_namespace}/services/http:tenant-workload:8080/proxy/ledger?since={since}"
        )
        if until is not None:
            path += f"&until={until}"
        return json.loads(self.command("get", "--raw", path, timeout=60))

    def object_audit(self, writes: dict[str, list[tuple[str, str]]]) -> dict:
        """Read back every canary and the given acknowledged writes; report the object ring."""
        request = json.dumps({"canaries_per_tenant": self.tier.canaries_per_tenant, "writes": writes})
        output = self.tenant_command(
            "exec",
            "-i",
            "deploy/region-verifier",
            "-c",
            "object-verifier",
            "--",
            "python",
            "/workload/object_audit.py",
            input_text=request,
            timeout=600,
        )
        return json.loads(output)

    def workload_status(self) -> dict:
        path = f"/api/v1/namespaces/{self.tenant_namespace}/services/http:tenant-workload:8080/proxy/status"
        return json.loads(self.command("get", "--raw", path, timeout=60))

    # The tenant workload runs continuously from deploy; these keep the
    # WorkloadApplication contract that the conductor calls.
    def create_workload(self, **kwargs):
        return None

    def start_workload(self):
        return None

    def stop_workload(self):
        if self.tenant_command("get", "deployment", "tenant-workload", "--ignore-not-found", "-o", "name").strip():
            self.tenant_command("scale", "deployment/tenant-workload", "--replicas=0")

    def cleanup(self):
        for namespace in (self.tenant_namespace, self.namespace):
            subprocess.run(
                ["kubectl", "delete", "namespace", namespace, "--ignore-not-found", "--wait=true", "--timeout=300s"],
                capture_output=True,
                text=True,
                timeout=320,
            )

    def delete(self):
        self.cleanup()


def _dump(documents):
    import yaml

    return yaml.safe_dump_all(documents, sort_keys=False)
