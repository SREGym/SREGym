"""Executable scale tiers for the existing DeathStarBench applications.

Render the upstream manifests without modifying the applications submodule.
MongoDB 4.4 is intentional: HotelReservation's mgo client uses legacy opcodes.
These are workstation topologies, not simulations of geographic regions.
"""

import copy
import json
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from sregym.paths import TARGET_MICROSERVICES
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.apps.social_network import SocialNetwork

MONGO_IMAGE = "mongo:4.4.29"
LABEL = "sregym.io/database"


@dataclass(frozen=True)
class ScaleTier:
    members: int
    service_replicas: int
    workload_rate: int


SCALE_TIERS = {
    "single": ScaleTier(1, 1, 10),
    "replicated": ScaleTier(3, 2, 20),
    "expanded": ScaleTier(5, 3, 30),
}


def tier_config(tier: str) -> ScaleTier:
    if tier not in SCALE_TIERS:
        raise ValueError(f"Unknown DeathStarBench tier {tier!r}; choose from {tuple(SCALE_TIERS)}")
    return SCALE_TIERS[tier]


def member_hosts(name: str, namespace: str, members: int) -> list[str]:
    return [f"{name}-{i}.{name}-members.{namespace}.svc.cluster.local:27017" for i in range(members)]


def mongo_resources(name: str, namespace: str, members: int, authenticated: bool, storage_class: str) -> list[dict]:
    labels = {LABEL: name}
    probe = {
        "exec": {"command": ["mongo", "--quiet", "--eval", "quit(db.adminCommand({ping:1}).ok ? 0 : 1)"]},
        "periodSeconds": 10,
        "timeoutSeconds": 5,
        "failureThreshold": 6,
    }
    container = {
        "name": "mongodb",
        "image": MONGO_IMAGE,
        "args": ["--replSet", name, "--bind_ip_all", "--wiredTigerCacheSizeGB", "0.25", "--oplogSize", "128"],
        "ports": [{"containerPort": 27017, "name": "mongodb"}],
        "resources": {"requests": {"cpu": "100m", "memory": "384Mi"}, "limits": {"cpu": "1", "memory": "768Mi"}},
        "volumeMounts": [{"name": "data", "mountPath": "/data/db"}],
        "readinessProbe": probe,
        "startupProbe": {**copy.deepcopy(probe), "failureThreshold": 90},
    }
    spec = {
        "automountServiceAccountToken": False,
        "containers": [container],
        "affinity": {
            "podAntiAffinity": {
                "preferredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "weight": 100,
                        "podAffinityTerm": {
                            "topologyKey": "kubernetes.io/hostname",
                            "labelSelector": {"matchLabels": labels},
                        },
                    }
                ]
            }
        },
    }
    if authenticated:
        container["args"] += ["--keyFile", "/key/keyfile"]
        container["volumeMounts"].append({"name": "key", "mountPath": "/key", "readOnly": True})
        spec["volumes"] = [
            {"name": "key", "emptyDir": {}},
            {"name": "source-key", "secret": {"secretName": "mongodb-membership"}},
        ]
        spec["initContainers"] = [
            {
                "name": "prepare-key",
                "image": "busybox:1.36",
                "command": [
                    "sh",
                    "-ec",
                    "cp /source/keyfile /key/keyfile; chown 999:999 /key/keyfile; chmod 400 /key/keyfile",
                ],
                "volumeMounts": [
                    {"name": "source-key", "mountPath": "/source", "readOnly": True},
                    {"name": "key", "mountPath": "/key"},
                ],
            }
        ]
    return [
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": f"{name}-members"},
            "spec": {
                "clusterIP": "None",
                "publishNotReadyAddresses": True,
                "selector": labels,
                "ports": [{"port": 27017, "targetPort": "mongodb"}],
            },
        },
        {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": {"name": name},
            "spec": {
                "serviceName": f"{name}-members",
                "replicas": members,
                "podManagementPolicy": "Parallel",
                "selector": {"matchLabels": labels},
                "template": {"metadata": {"labels": labels}, "spec": spec},
                "volumeClaimTemplates": [
                    {
                        "metadata": {"name": "data"},
                        "spec": {
                            "accessModes": ["ReadWriteOnce"],
                            "storageClassName": storage_class,
                            "resources": {"requests": {"storage": "2Gi"}},
                        },
                    }
                ],
            },
        },
        {
            "apiVersion": "policy/v1",
            "kind": "PodDisruptionBudget",
            "metadata": {"name": name},
            "spec": {"minAvailable": members // 2 + 1, "selector": {"matchLabels": labels}},
        },
    ]


class ScaledApplication:
    """Shared lifecycle: storage -> elections -> seed one app replica -> scale out."""

    authenticated_databases = frozenset()

    def configure_scale(self, tier: str, storage_class: str):
        self.scale = tier_config(tier)
        self.scale_tier = tier
        self.storage_class = storage_class
        self.helm_deploy = False
        self.mount_failure_scripts = False

    def get_app_json(self):
        metadata = super().get_app_json()
        if hasattr(self, "scale_tier"):
            metadata["Desc"] += (
                f" DeathStarBench 2.0 tier {self.scale_tier}: {self.scale.members} persistent MongoDB members per "
                f"database and {self.scale.service_replicas} replicas per application service. "
                "Database members have stable DNS names and independent volumes; clients discover the elected primary."
            )
        return metadata

    def command(self, *args: str, input_text: str | None = None, timeout: int = 120) -> str:
        return subprocess.run(
            ["kubectl", "-n", self.namespace, *args],
            input=input_text,
            text=True,
            capture_output=True,
            check=True,
            timeout=timeout,
        ).stdout

    def apply(self, documents: list[dict]):
        if documents:
            self.command("apply", "-f", "-", input_text=yaml.safe_dump_all(documents, sort_keys=False))

    def mongo(self, name: str, script: str, *, member: int = 0, authenticate: bool = True) -> str:
        args = ["exec", f"{name}-{member}", "-c", "mongodb", "--", "mongo", "--quiet"]
        if authenticate and name in self.authenticated_databases:
            args += ["-u", "admin", "-p", "admin", "--authenticationDatabase", "admin"]
        return self.command(*args, "--eval", script, timeout=40)

    def mongo_primary(self, name: str, script: str) -> str:
        hosts = ",".join(member_hosts(name, self.namespace, self.scale.members))
        auth = "admin:admin@" if name in self.authenticated_databases else ""
        uri = f"mongodb://{auth}{hosts}/admin?replicaSet={name}&connectTimeoutMS=3000&serverSelectionTimeoutMS=5000"
        return self.command(
            "exec", f"{name}-0", "-c", "mongodb", "--", "mongo", "--quiet", uri, "--eval", script, timeout=40
        )

    def _bootstrap_database(self, name: str):
        config = {
            "_id": name,
            "members": [
                {"_id": i, "host": host, "priority": 2 if i == 0 else 1}
                for i, host in enumerate(member_hosts(name, self.namespace, self.scale.members))
            ],
        }
        # Only initialize a fresh set. Redeploying must never force reconfiguration or erase data.
        script = f"""
        var status = db.adminCommand({{replSetGetStatus:1}});
        if (status.code === 94) {{ assert.commandWorked(rs.initiate({json.dumps(config)})); }}
        else if (!status.ok && status.code !== 13) {{ throw new Error(tojson(status)); }}
        """
        self.mongo(name, script, authenticate=False)
        if name in self.authenticated_databases:
            script = """
            var admin = db.getSiblingDB('admin');
            if (!admin.auth('admin', 'admin')) {
                assert.soon(function() { return db.isMaster().ismaster; }, 'primary election', 120000);
                admin.createUser({user:'admin', pwd:'admin', roles:['root']});
            }
            """
            # Election loops live inside mongo; give this one bootstrap a longer budget.
            self.command("exec", f"{name}-0", "-c", "mongodb", "--", "mongo", "--quiet", "--eval", script, timeout=150)
        self.wait_database(name)

    def wait_database(self, name: str, timeout: int = 180):
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            try:
                self.mongo(
                    name,
                    f"""
                    var s = rs.status(); assert.commandWorked(s);
                    assert.eq({self.scale.members}, s.members.length);
                    assert.eq(1, s.members.filter(function(m) {{ return m.state === 1 && m.health === 1; }}).length);
                    assert(s.members.every(function(m) {{ return m.health === 1 && (m.state === 1 || m.state === 2); }}));
                """,
                )
                return
            except subprocess.SubprocessError as exc:
                last = str(exc)
                time.sleep(3)
        raise RuntimeError(f"Replica set {name} did not become healthy: {last}")

    def render(self) -> list[dict]:
        documents = self.source_documents()
        databases = sorted(
            d["metadata"]["name"]
            for d in documents
            if d.get("kind") == "Deployment"
            and any(c["image"].split(":")[0].endswith("mongo") for c in d["spec"]["template"]["spec"]["containers"])
        )
        if len(databases) != self.expected_databases:
            raise ValueError(f"Expected {self.expected_databases} MongoDB deployments; found {databases}")
        self.databases = databases
        self.patch_clients(documents)
        rendered = []
        for doc in documents:
            kind, name = doc.get("kind"), doc.get("metadata", {}).get("name")
            if kind in {"PersistentVolume", "PersistentVolumeClaim"}:
                continue  # Replace upstream hostPath/shared claims with one dynamically provisioned PVC per member.
            if kind == "Deployment" and name in databases:
                rendered.extend(
                    mongo_resources(
                        name,
                        self.namespace,
                        self.scale.members,
                        name in self.authenticated_databases,
                        self.storage_class,
                    )
                )
                continue
            if kind == "Service" and name in databases:
                doc["spec"]["selector"] = {LABEL: name}
            if kind == "Deployment" and self.is_application_service(name):
                doc["spec"]["replicas"] = self.scale.service_replicas
                pod = doc["spec"]["template"]["spec"]
                labels = doc["spec"]["selector"]["matchLabels"]
                pod["topologySpreadConstraints"] = [
                    {
                        "maxSkew": 1,
                        "topologyKey": "kubernetes.io/hostname",
                        "whenUnsatisfiable": "ScheduleAnyway",
                        "labelSelector": {"matchLabels": labels},
                    }
                ]
                for container in pod["containers"]:
                    port = container.get("ports", [{}])[0].get("containerPort")
                    if port:
                        container.setdefault(
                            "readinessProbe", {"tcpSocket": {"port": port}, "periodSeconds": 5, "timeoutSeconds": 3}
                        )
                    container.setdefault(
                        "resources", {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "512Mi"}}
                    )
            rendered.append(doc)
        rendered.append(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": "application-client"},
                "spec": {
                    "automountServiceAccountToken": False,
                    "containers": [
                        {
                            "name": "client",
                            "image": "python:3.12-alpine",
                            "command": ["sleep", "infinity"],
                            "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}},
                        }
                    ],
                },
            }
        )
        return rendered

    def check_workflow(self):
        script = Path(__file__).with_name("deathstarbench_workflow.py").read_text()
        output = self.command(
            "exec", "-i", "application-client", "--", "python", "-", self.namespace, input_text=script, timeout=120
        )
        token = json.loads(output)["token"]
        if self.namespace == "hotel-reservation":
            self.mongo_primary(
                "mongodb-reservation",
                f"""
                assert(db.getSiblingDB('reservation-db').reservation.findOne({{customerName:{json.dumps(token)}}}));
            """,
            )
        else:
            self.mongo_primary(
                "post-storage-mongodb",
                f"""
                assert(db.getSiblingDB('post').post.findOne({{text:{json.dumps(token)}}}));
            """,
            )
        return output

    def deploy(self):
        self.create_namespace()
        self.command("get", "storageclass", self.storage_class)
        documents = self.render()
        if self.authenticated_databases:
            existing = self.command("get", "secret", "mongodb-membership", "--ignore-not-found", "-o", "name")
            if not existing.strip():
                self.apply(
                    [
                        {
                            "apiVersion": "v1",
                            "kind": "Secret",
                            "metadata": {"name": "mongodb-membership"},
                            "stringData": {"keyfile": secrets.token_urlsafe(96).replace("-", "a").replace("_", "b")},
                        }
                    ]
                )
        infrastructure = [d for d in documents if d.get("kind") != "Deployment"]
        self.apply(infrastructure)
        for name in self.databases:
            self.command("rollout", "status", f"statefulset/{name}", "--timeout=900s", timeout=920)
            self._bootstrap_database(name)
        deployments = [d for d in documents if d.get("kind") == "Deployment"]
        first = copy.deepcopy(deployments)
        for d in first:
            d["spec"]["replicas"] = 1
        self.apply(first)
        self.kubectl.wait_for_ready(self.namespace)
        self.seed_data()
        self.apply(deployments)
        for d in deployments:
            self.command("rollout", "status", f"deployment/{d['metadata']['name']}", "--timeout=600s", timeout=620)
        self.check_workflow()

    def seed_data(self):
        """Optional initial business data, after services start and before scaling."""

    def cleanup(self):
        from sregym.service.apps.persistent import cleanup_persistent_namespace

        self.stop_workload()
        cleanup_persistent_namespace(self)

    def delete(self):
        self.cleanup()

    def create_workload(self, **kwargs):
        kwargs.setdefault("rate", self.scale.workload_rate)
        return super().create_workload(**kwargs)


class ScaledHotelReservation(ScaledApplication, HotelReservation):
    expected_databases = 6
    authenticated_databases = frozenset({"mongodb-geo", "mongodb-rate"})
    services = frozenset({"frontend", "geo", "profile", "rate", "recommendation", "reservation", "search", "user"})

    def __init__(self, tier: str = "replicated", storage_class: str = "standard"):
        tier_config(tier)
        super().__init__(mount_failure_scripts=False)
        self.configure_scale(tier, storage_class)

    def is_application_service(self, name):
        return name in self.services

    def source_documents(self):
        return [
            d
            for p in sorted(Path(self.k8s_deploy_path).rglob("*.yaml"))
            for d in yaml.safe_load_all(p.read_text())
            if isinstance(d, dict) and "kind" in d
        ]

    def patch_clients(self, documents):
        config = json.loads((TARGET_MICROSERVICES / "hotelReservation/config.json").read_text())
        for key, value in config.items():
            if key.endswith("MongoAddress"):
                name = value.split(":")[0]
                config[key] = ",".join(member_hosts(name, self.namespace, self.scale.members)) + f"/?replicaSet={name}"
        documents.append(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "hotel-runtime-config"},
                "data": {"config.json": json.dumps(config, indent=2)},
            }
        )
        for doc in documents:
            if doc.get("kind") == "Deployment" and self.is_application_service(doc["metadata"]["name"]):
                pod = doc["spec"]["template"]["spec"]
                pod.setdefault("volumes", []).append(
                    {"name": "runtime-config", "configMap": {"name": "hotel-runtime-config"}}
                )
                for c in pod["containers"]:
                    c.setdefault("volumeMounts", []).append(
                        {
                            "name": "runtime-config",
                            "subPath": "config.json",
                            "mountPath": "/go/src/github.com/harlow/go-micro-services/config.json",
                        }
                    )


class ScaledSocialNetwork(ScaledApplication, SocialNetwork):
    expected_databases = 6

    def __init__(self, tier: str = "replicated", storage_class: str = "standard"):
        tier_config(tier)
        super().__init__()
        self.configure_scale(tier, storage_class)

    def is_application_service(self, name):
        return name.endswith("-service") or name in {"nginx-thrift", "media-frontend"}

    def seed_data(self):
        marker = self.command("get", "configmap", "business-data-seeded", "--ignore-not-found", "-o", "name")
        if marker.strip():
            return  # A normal redeployment must not replace or repair incident state.
        script = Path(__file__).with_name("deathstarbench_seed.py").read_text()
        output = self.command("exec", "-i", "application-client", "--", "python", "-", input_text=script, timeout=600)
        counts = json.loads(output)
        self.mongo_primary("user-mongodb", f"assert.eq({counts['users']},db.getSiblingDB('user').user.count());")
        self.mongo_primary(
            "post-storage-mongodb",
            f"assert.eq({counts['posts']},db.getSiblingDB('post').post.count({{text:/^sregym-seed-/}}));",
        )
        self.apply(
            [
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "business-data-seeded"},
                    "data": {"counts.json": json.dumps(counts)},
                }
            ]
        )

    def source_documents(self):
        result = subprocess.run(
            [
                "helm",
                "template",
                self.helm_configs["release_name"],
                self.helm_configs["chart_path"],
                "--namespace",
                self.namespace,
            ],
            check=True,
            text=True,
            capture_output=True,
            timeout=120,
        )
        return [d for d in yaml.safe_load_all(result.stdout) if isinstance(d, dict)]

    def patch_clients(self, documents):
        for doc in documents:
            if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "media-frontend":
                # Its upstream Service and nginx.conf use 8080; the declared
                # container port is stale and would produce a broken readiness probe.
                for container in doc["spec"]["template"]["spec"]["containers"]:
                    if container["name"] == "media-frontend":
                        container["ports"] = [{"containerPort": 8080}]
            if doc.get("kind") == "ConfigMap" and "service-config.json" in doc.get("data", {}):
                config = json.loads(doc["data"]["service-config.json"])
                for key, value in config.items():
                    if key in self.databases:
                        hosts = member_hosts(key, self.namespace, self.scale.members)
                        # The upstream C++ client appends :port to addr, then discovers the set via isMaster.
                        value["addr"] = ",".join([*hosts[:-1], hosts[-1].removesuffix(":27017")])
                doc["data"]["service-config.json"] = json.dumps(config, indent=2)
