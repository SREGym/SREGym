"""Configuration files for the OpenStack control plane.

Every endpoint is a Kubernetes Service name qualified with the namespace, so the
service catalog also resolves from the tenant namespace.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping

REGION = "RegionOne"


def ini(sections: Mapping[str, Mapping[str, object]]) -> str:
    lines: list[str] = []
    for section, options in sections.items():
        lines.append(f"[{section}]")
        lines.extend(f"{key} = {value}" for key, value in options.items())
        lines.append("")
    return "\n".join(lines)


class Endpoints:
    def __init__(self, namespace: str):
        self.namespace = namespace

    def host(self, service: str) -> str:
        return f"{service}.{self.namespace}.svc"

    @property
    def keystone(self) -> str:
        return f"http://{self.host('keystone')}:5000"

    @property
    def nova(self) -> str:
        return f"http://{self.host('nova-api')}:8774/v2.1"

    @property
    def placement(self) -> str:
        return f"http://{self.host('placement')}:8778"

    @property
    def glance(self) -> str:
        return f"http://{self.host('glance')}:9292"

    @property
    def neutron(self) -> str:
        return f"http://{self.host('neutron')}:9696"

    @property
    def swift(self) -> str:
        return f"http://{self.host('swift-proxy')}:8080/v1/AUTH_%(project_id)s"

    @property
    def memcached(self) -> str:
        return f"{self.host('memcached')}:11211"

    def database(self, user: str, password: str, database: str) -> str:
        return f"mysql+pymysql://{user}:{password}@{self.host('mariadb')}/{database}?charset=utf8"

    def broker(self, user: str, password: str) -> str:
        return f"rabbit://{user}:{password}@{self.host('rabbitmq')}:5672/"


def authtoken(endpoints: Endpoints, user: str, password: str) -> dict:
    return {
        "www_authenticate_uri": endpoints.keystone,
        "auth_url": endpoints.keystone,
        "memcached_servers": endpoints.memcached,
        "auth_type": "password",
        "project_domain_name": "Default",
        "user_domain_name": "Default",
        "project_name": "service",
        "username": user,
        "password": password,
        "region_name": REGION,
    }


def service_client(endpoints: Endpoints, user: str, password: str) -> dict:
    return {
        "auth_type": "password",
        "auth_url": f"{endpoints.keystone}/v3",
        "project_domain_name": "Default",
        "user_domain_name": "Default",
        "project_name": "service",
        "username": user,
        "password": password,
        "region_name": REGION,
        "valid_interfaces": "internal,public",
    }


def keystone_conf(endpoints: Endpoints, passwords: Mapping[str, str]) -> str:
    return ini(
        {
            "DEFAULT": {"log_file": "", "use_stderr": "true"},
            "database": {"connection": endpoints.database("keystone", passwords["db_keystone"], "keystone")},
            "token": {"provider": "fernet", "expiration": 3600},
            "fernet_tokens": {"key_repository": "/etc/keystone/fernet-keys", "max_active_keys": 3},
            "credential": {"key_repository": "/etc/keystone/credential-keys"},
            "cache": {
                "enabled": "true",
                "backend": "oslo_cache.memcache_pool",
                "memcache_servers": endpoints.memcached,
            },
        }
    )


def placement_conf(endpoints: Endpoints, passwords: Mapping[str, str]) -> str:
    return ini(
        {
            "DEFAULT": {"use_stderr": "true"},
            "placement_database": {
                "connection": endpoints.database("placement", passwords["db_placement"], "placement")
            },
            "api": {"auth_strategy": "keystone"},
            "keystone_authtoken": authtoken(endpoints, "placement", passwords["placement"]),
        }
    )


def glance_conf(endpoints: Endpoints, passwords: Mapping[str, str]) -> str:
    return ini(
        {
            "DEFAULT": {"use_stderr": "true", "bind_port": 9292, "enabled_backends": "file:file", "workers": 1},
            "database": {"connection": endpoints.database("glance", passwords["db_glance"], "glance")},
            "glance_store": {"default_backend": "file"},
            "file": {"filesystem_store_datadir": "/var/lib/glance/images/"},
            "keystone_authtoken": authtoken(endpoints, "glance", passwords["glance"]),
            "paste_deploy": {"flavor": "keystone"},
            "os_glance_staging_store": {"filesystem_store_datadir": "/var/lib/glance/staging/"},
            "os_glance_tasks_store": {"filesystem_store_datadir": "/var/lib/glance/tasks/"},
        }
    )


def neutron_conf(endpoints: Endpoints, passwords: Mapping[str, str]) -> str:
    """API and RPC server only. Hypervisors run the fake virt driver and bind no ports."""
    return ini(
        {
            "DEFAULT": {
                "use_stderr": "true",
                "core_plugin": "ml2",
                # Loads the router and port-forwarding models that ML2 port queries join.
                "service_plugins": "router,port_forwarding",
                "auth_strategy": "keystone",
                "transport_url": endpoints.broker("openstack", passwords["rabbit_openstack"]),
                "api_workers": 1,
                "rpc_workers": 1,
                "notify_nova_on_port_status_changes": "false",
                "notify_nova_on_port_data_changes": "false",
            },
            "database": {"connection": endpoints.database("neutron", passwords["db_neutron"], "neutron")},
            "keystone_authtoken": authtoken(endpoints, "neutron", passwords["neutron"]),
            "oslo_concurrency": {"lock_path": "/var/lib/neutron/tmp"},
            "ml2": {
                "type_drivers": "flat",
                "tenant_network_types": "",
                "mechanism_drivers": "",
                "extension_drivers": "",
            },
            "securitygroup": {"enable_security_group": "true"},
        }
    )


def nova_conf(endpoints: Endpoints, passwords: Mapping[str, str]) -> str:
    """Shared by every Nova service. Hypervisors override host and transport_url."""
    return ini(
        {
            "DEFAULT": {
                "use_stderr": "true",
                "transport_url": endpoints.broker("openstack", passwords["rabbit_openstack"]),
                "osapi_compute_workers": 2,
                "metadata_workers": 1,
                # Fake hypervisors still run the real resource tracker.
                "compute_driver": "fake.MediumFakeDriver",
                "update_resources_interval": 60,
                "heal_instance_info_cache_interval": -1,
            },
            "api": {"auth_strategy": "keystone"},
            "api_database": {"connection": endpoints.database("nova", passwords["db_nova"], "nova_api")},
            "database": {"connection": endpoints.database("nova", passwords["db_nova"], "nova")},
            "keystone_authtoken": authtoken(endpoints, "nova", passwords["nova"]),
            "placement": service_client(endpoints, "placement", passwords["placement"]),
            "glance": {"endpoint_override": endpoints.glance},
            "neutron": service_client(endpoints, "neutron", passwords["neutron"]),
            "scheduler": {"discover_hosts_in_cells_interval": 30, "max_attempts": 3},
            "conductor": {"workers": 2},
            "oslo_concurrency": {"lock_path": "/var/lib/nova/tmp"},
            "oslo_messaging_rabbit": {"heartbeat_timeout_threshold": 60},
            "vnc": {"enabled": "false"},
            "wsgi": {"api_paste_config": "/etc/nova/api-paste.ini"},
        }
    )


def rabbitmq_conf() -> str:
    return "\n".join(
        [
            "listeners.tcp.default = 5672",
            "management.tcp.port = 15672",
            "loopback_users = none",
            "definitions.import_backend = local_filesystem",
            "definitions.local.path = /etc/rabbitmq/definitions.json",
            "log.console = true",
            "log.console.level = info",
            "log.file = false",
            "",
        ]
    )


def rabbitmq_password_hash(password: str, salt: bytes) -> str:
    """RabbitMQ's rabbit_password_hashing_sha256 format, for boot-time definitions."""
    return base64.b64encode(salt + hashlib.sha256(salt + password.encode()).digest()).decode()


def rabbitmq_definitions(passwords: Mapping[str, str], salt: bytes) -> str:
    """Users the broker must have after any restart. Hypervisor users are not here:
    the host-access reconciler owns them."""
    users = [("admin", passwords["rabbit_admin"], ["administrator"]), ("openstack", passwords["rabbit_openstack"], [])]
    return json.dumps(
        {
            "users": [
                {
                    "name": name,
                    "password_hash": rabbitmq_password_hash(password, salt),
                    "hashing_algorithm": "rabbit_password_hashing_sha256",
                    "tags": tags,
                }
                for name, password, tags in users
            ],
            "vhosts": [{"name": "/"}],
            "permissions": [
                {"user": name, "vhost": "/", "configure": ".*", "write": ".*", "read": ".*"} for name, _, _ in users
            ],
        },
        indent=2,
    )


def clouds_yaml(endpoints: Endpoints, admin_password: str) -> str:
    return "\n".join(
        [
            "clouds:",
            "  admin:",
            "    auth:",
            f"      auth_url: {endpoints.keystone}/v3",
            "      username: admin",
            f"      password: {admin_password}",
            "      project_name: admin",
            "      project_domain_name: Default",
            "      user_domain_name: Default",
            f"    region_name: {REGION}",
            "    interface: internal",
            "    identity_api_version: 3",
            "",
        ]
    )


SWIFT_PORTS = {"object": 6200, "container": 6201, "account": 6202}


def swift_conf(prefix: str, suffix: str) -> str:
    return ini(
        {
            "swift-hash": {"swift_hash_path_prefix": prefix, "swift_hash_path_suffix": suffix},
            "storage-policy:0": {"name": "Policy-0", "default": "yes"},
        }
    )


def _swift_logging() -> dict:
    # No syslog in the pod: servers also log to the console with -v.
    return {"log_udp_host": "127.0.0.1", "log_udp_port": 514, "log_level": "INFO"}


def swift_proxy_conf(endpoints: Endpoints, passwords: Mapping[str, str]) -> str:
    return ini(
        {
            "DEFAULT": {"bind_port": 8080, "workers": 2, "swift_dir": "/etc/swift", **_swift_logging()},
            "pipeline:main": {
                "pipeline": "catch_errors gatekeeper healthcheck proxy-logging cache listing_formats "
                "authtoken keystoneauth copy dlo slo proxy-logging proxy-server"
            },
            "app:proxy-server": {"use": "egg:swift#proxy", "account_autocreate": "true"},
            "filter:authtoken": {
                "paste.filter_factory": "keystonemiddleware.auth_token:filter_factory",
                **authtoken(endpoints, "swift", passwords["swift"]),
                "delay_auth_decision": "True",
            },
            "filter:keystoneauth": {
                "use": "egg:swift#keystoneauth",
                "operator_roles": "admin, member",
                "reseller_admin_role": "ResellerAdmin",
            },
            "filter:cache": {"use": "egg:swift#memcache", "memcache_servers": endpoints.memcached},
            **{
                f"filter:{name}": {"use": f"egg:swift#{name.replace('-', '_')}"}
                for name in (
                    "catch_errors",
                    "gatekeeper",
                    "healthcheck",
                    "proxy-logging",
                    "listing_formats",
                    "copy",
                    "dlo",
                    "slo",
                )
            },
        }
    )


def swift_server_conf(kind: str, replicator_interval_s: int) -> str:
    """account-, container- or object-server.conf, with that ring's background daemons."""
    sections = {
        "DEFAULT": {
            "bind_ip": "0.0.0.0",
            "bind_port": SWIFT_PORTS[kind],
            "devices": "/srv/node",
            "mount_check": "false",
            "user": "swift",
            "workers": 1,
            "swift_dir": "/etc/swift",
            **_swift_logging(),
        },
        "pipeline:main": {"pipeline": f"healthcheck recon {kind}-server"},
        f"app:{kind}-server": {"use": f"egg:swift#{kind}"},
        "filter:healthcheck": {"use": "egg:swift#healthcheck"},
        "filter:recon": {"use": "egg:swift#recon", "recon_cache_path": "/var/cache/swift"},
        f"{kind}-replicator": {"interval": replicator_interval_s},
    }
    if kind != "account":
        sections[f"{kind}-updater"] = {}
    if kind == "object":
        sections["object-auditor"] = {}
    return ini(sections)


def rsyncd_conf() -> str:
    modules = "\n".join(
        f"[{kind}]\nmax connections = 8\npath = /srv/node/\nread only = false\nlock file = /var/lock/{kind}.lock\n"
        for kind in SWIFT_PORTS
    )
    return (
        "uid = swift\ngid = swift\nlog file = /dev/stdout\npid file = /var/run/rsyncd.pid\naddress = 0.0.0.0\n\n"
        + modules
    )
