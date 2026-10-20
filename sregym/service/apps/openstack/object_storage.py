"""Swift object storage for the region: storage nodes, a proxy, and the operators' ring builder.

One storage node per rack, each its own zone. Account and container rings keep
three replicas; the object ring's replica count is the application's
``object_replicas`` (three unless a family sets it). The ring builder
pod holds the builder files (and the backups ``swift-ring-builder`` writes on
every change) on a persistent volume and serves the current rings over HTTP. A
sidecar in every proxy and storage pod installs whatever ring it serves, so a
ring change reaches the whole cluster within seconds, as a ring push does.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

from sregym.service.apps.openstack import config

# Kolla stopped publishing Swift images after March 2025; pin the last build.
SWIFT_IMAGE = (
    "quay.io/openstack.kolla/swift-proxy-server@sha256:1705be83d419244e5c42cea2bd52e187951dc01e0042f19a9053fd0027741513"
)
BUILDER_DIR = "/etc/swift-builders"
RUNTIME_DIR = Path(__file__).with_name("runtime")
SWIFT_ETC = {"name": "swift-etc", "mountPath": "/etc/swift"}
RUNTIME = {"name": "runtime", "mountPath": "/runtime"}


def devices(tier) -> list[str]:
    return [f"d{index}" for index in range(tier.swift_devices_per_node)]


def node_host(app, node: str) -> str:
    return f"{node}.swift-storage.{app.namespace}.svc"


def config_files(app) -> dict[str, str]:
    p = app.passwords
    interval = 30
    return {
        "swift.conf": config.swift_conf(p["swift_hash_prefix"], p["swift_hash_suffix"]),
        "proxy-server.conf": config.swift_proxy_conf(app.endpoints, p),
        "account-server.conf": config.swift_server_conf("account", interval),
        "container-server.conf": config.swift_server_conf("container", interval),
        "object-server.conf": config.swift_server_conf("object", interval),
        "rsyncd.conf": config.rsyncd_conf(),
    }


def _install_config() -> dict:
    # Swift wants its configuration and rings side by side in /etc/swift, and
    # the ring sidecar replaces ring files there, so /etc/swift is an emptyDir.
    return {
        "name": "install-config",
        "image": SWIFT_IMAGE,
        "command": ["sh", "-c", "cp /swift-config/* /etc/swift/ && python /runtime/ring_sync.py --once"],
        "env": [{"name": "RING_SOURCE", "value": "http://swift-rings:8000"}],
        "volumeMounts": [SWIFT_ETC, RUNTIME, {"name": "swift-config", "mountPath": "/swift-config"}],
    }


def _ring_sync() -> dict:
    return {
        "name": "ring-sync",
        "image": SWIFT_IMAGE,
        "command": ["python", "/runtime/ring_sync.py"],
        "env": [{"name": "RING_SOURCE", "value": "http://swift-rings:8000"}],
        "volumeMounts": [SWIFT_ETC, RUNTIME],
        "resources": {"requests": {"cpu": "5m", "memory": "24Mi"}, "limits": {"memory": "128Mi"}},
    }


def _volumes() -> list[dict]:
    return [
        {"name": "swift-etc", "emptyDir": {}},
        {"name": "swift-config", "secret": {"secretName": "swift-config"}},
        {"name": "runtime", "configMap": {"name": "openstack-runtime"}},
    ]


def render(app) -> list[dict]:
    tier = app.tier
    storage_labels = app.labels("swift-storage")
    claim = {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}}
    if app.storage_class:
        claim["storageClassName"] = app.storage_class
    return [
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "swift-builders"}, "spec": claim},
        app.service("swift-rings", "swift-ring-builder", [8000]),
        _with_recreate(
            app.deployment(
                "swift-ring-builder",
                {
                    "volumes": [
                        {"name": "builders", "persistentVolumeClaim": {"claimName": "swift-builders"}},
                        {"name": "runtime", "configMap": {"name": "openstack-runtime"}},
                    ],
                    "containers": [
                        {
                            "name": "ring-builder",
                            "image": SWIFT_IMAGE,
                            # Serves the builder directory: the rings it holds
                            # are the rings the cluster runs.
                            "command": ["python", "-m", "http.server", "8000", "--directory", BUILDER_DIR],
                            "workingDir": BUILDER_DIR,
                            "ports": [{"containerPort": 8000}],
                            "volumeMounts": [{"name": "builders", "mountPath": BUILDER_DIR}, RUNTIME],
                            "resources": {
                                "requests": {"cpu": "10m", "memory": "64Mi"},
                                "limits": {"memory": "512Mi"},
                            },
                        }
                    ],
                },
            )
        ),
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "swift-storage"},
            "spec": {
                "clusterIP": "None",
                # Ring addresses must resolve before a node is ready to serve.
                "publishNotReadyAddresses": True,
                "selector": storage_labels,
                "ports": [{"name": kind, "port": port} for kind, port in config.SWIFT_PORTS.items()]
                + [{"name": "rsync", "port": 873}],
            },
        },
        {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": {"name": "swift-storage", "labels": storage_labels},
            "spec": {
                "serviceName": "swift-storage",
                "replicas": len(tier.storage_nodes),
                "podManagementPolicy": "Parallel",
                "selector": {"matchLabels": storage_labels},
                "template": {
                    "metadata": {"labels": storage_labels},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "enableServiceLinks": False,
                        "volumes": [*_volumes(), {"name": "srv", "emptyDir": {}}],
                        "initContainers": [_install_config()],
                        "containers": [
                            {
                                "name": "storage",
                                "image": SWIFT_IMAGE,
                                "command": ["bash", "/runtime/swift_storage.sh"],
                                "env": [{"name": "DEVICES", "value": " ".join(devices(tier))}],
                                "securityContext": {"runAsUser": 0},
                                "volumeMounts": [SWIFT_ETC, RUNTIME, {"name": "srv", "mountPath": "/srv/node"}],
                                "readinessProbe": {
                                    "httpGet": {"path": "/healthcheck", "port": config.SWIFT_PORTS["object"]},
                                    "periodSeconds": 5,
                                },
                                "resources": {
                                    "requests": {"cpu": "30m", "memory": "192Mi"},
                                    "limits": {"memory": "768Mi"},
                                },
                            },
                            _ring_sync(),
                        ],
                    },
                },
            },
        },
        app.service("swift-proxy", "swift-proxy", [8080]),
        app.deployment(
            "swift-proxy",
            {
                "volumes": _volumes(),
                "initContainers": [_install_config()],
                "containers": [
                    {
                        "name": "proxy",
                        "image": SWIFT_IMAGE,
                        "command": ["swift-proxy-server", "/etc/swift/proxy-server.conf", "-v"],
                        "ports": [{"containerPort": 8080}],
                        "volumeMounts": [SWIFT_ETC],
                        "readinessProbe": {"httpGet": {"path": "/healthcheck", "port": 8080}, "periodSeconds": 5},
                        "resources": {"requests": {"cpu": "30m", "memory": "128Mi"}, "limits": {"memory": "512Mi"}},
                    },
                    _ring_sync(),
                ],
            },
        ),
    ]


def _with_recreate(deployment: dict) -> dict:
    # The builder volume is ReadWriteOnce; never run two builders at once.
    deployment["spec"]["strategy"] = {"type": "Recreate"}
    return deployment


def config_secret(app) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "swift-config"},
        "stringData": config_files(app),
    }


def build_rings(app) -> dict:
    arguments = {
        "nodes": [node_host(app, node) for node in app.tier.storage_nodes],
        "devices": devices(app.tier),
        "part_power": app.tier.swift_part_power,
        "object_replicas": app.object_replicas,
    }
    output = app.command(
        "exec", "deploy/swift-ring-builder", "--", "python", "/runtime/swift_rings.py", json.dumps(arguments)
    )
    return json.loads(output.strip().splitlines()[-1])


def deploy(app) -> None:
    app.apply([config_secret(app)])
    documents = render(app)
    app.apply(
        [d for d in documents if d["metadata"]["name"] in ("swift-builders", "swift-rings", "swift-ring-builder")]
    )
    app.rollout("deployment/swift-ring-builder")
    build_rings(app)
    app.command(
        "exec",
        "-i",
        "deploy/swift-ring-builder",
        "--",
        "sh",
        "-c",
        f"cat > {BUILDER_DIR}/RUNBOOK.md",
        input_text=(RUNTIME_DIR / "swift_runbook.md").read_text(),
    )
    app.apply(
        [d for d in documents if d["metadata"]["name"] not in ("swift-builders", "swift-rings", "swift-ring-builder")]
    )
    app.rollout("statefulset/swift-storage", timeout=900)
    app.rollout("deployment/swift-proxy")


def new_hash_secrets() -> dict[str, str]:
    return {"swift_hash_prefix": secrets.token_hex(8), "swift_hash_suffix": secrets.token_hex(8)}
