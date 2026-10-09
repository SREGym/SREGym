"""Owned multi-region application deployment; construction never touches a cluster."""

import base64
import datetime
import hashlib
import ipaddress
import json
import re
import secrets
import shlex
import tempfile
import time
from pathlib import Path
from uuid import UUID, uuid4

import yaml

from sregym.conductor.scenarios.codehub_contracts import LifecyclePhase, OwnedResource, RunInventory
from sregym.conductor.scenarios.codehub_regions import mysql_groups, region_values, regional_inventory
from sregym.conductor.scenarios.database_recovery import TIERS, ScaleTier
from sregym.paths import TARGET_MICROSERVICES
from sregym.service.apps.base import Application

MYSQL_BOOTSTRAP_IMAGE = (
    "docker.io/library/mysql:8.4.6@sha256:869218921e61d6c3c89820955d63cca42971f0e3e6c1e2792247bbd944ebc6e9"
)
DATABASE_LINK_SLICE_MAX_BYTES = 64 * 1024


class CodeHub(Application):
    def __init__(
        self,
        *,
        tier: ScaleTier | None = None,
        image: str = "codehub:local",
        chart_path: Path | None = None,
        kubectl=None,
        run_id: str | None = None,
        generation: int = 1,
        readiness_seconds: int = 300,
        webhook_hosts: tuple[str, ...] = (),
        webhook_egress: tuple[tuple[str, int], ...] = (),
        database_links: dict[tuple[str, str, str], int] | None = None,
        storage_class: str | None = None,
    ):
        super().__init__(Path(__file__).resolve().parents[1] / "metadata" / "codehub.json")
        self.load_app_json()
        self.tier = tier or TIERS["small"]
        self.database_links = dict(database_links or {})
        self._database_links_installed = False
        self._validate_database_links(self.database_links)
        self.image = image
        if storage_class is not None and (
            type(storage_class) is not str
            or not 1 <= len(storage_class) <= 253
            or any(
                not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", label) for label in storage_class.split(".")
            )
        ):
            raise ValueError("Storage class must be a bounded DNS subdomain")
        self.storage_class = storage_class
        self.chart_path = chart_path or TARGET_MICROSERVICES / "codehub" / "helm"
        self.kubectl = kubectl
        self.readiness_seconds = readiness_seconds
        if type(readiness_seconds) is not int or readiness_seconds < 1:
            raise ValueError("Readiness deadline must be a positive integer")
        self.webhook_hosts = webhook_hosts
        self.webhook_egress = webhook_egress
        for cidr, port in webhook_egress:
            ipaddress.ip_network(cidr)
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("Invalid webhook destination port")
        self.inventory = RunInventory(run_id or str(uuid4()), generation)
        self.deployment_owner = secrets.token_hex(16)
        self.regions = ()
        self.database_groups = ()
        self.namespaces = [f"codehub-region-{chr(97 + i)}" for i in range(self.tier.regions)]
        self.frontend_service = "gateway"
        self.frontend_port = 8080
        self._credentials: dict[str, str] | None = None
        self.gateway_certificates: dict[str, str] = {}

    def _client(self):
        if self.kubectl is None:
            from sregym.service.kubectl import KubeCtl

            self.kubectl = KubeCtl()
        return self.kubectl

    def _validate_boundary(self) -> None:
        from sregym.service.docker_runtime import rootless_workload_enabled, validate_rootless_boundary

        if not rootless_workload_enabled():
            raise RuntimeError("CodeHub requires the qualified rootless workload boundary")
        validate_rootless_boundary()

    def _record(self, kind: str, namespace: str, name: str, uid: str) -> None:
        resource = OwnedResource(self.inventory.run_id, kind, namespace, name, uid)
        if resource not in self.inventory.resources:
            self.inventory = self.inventory.with_resource(resource)

    def _create_namespace(self, namespace: str) -> None:
        # A conflict is a failure, never permission to adopt an existing namespace.
        result = self._client().core_v1_api.create_namespace(
            body={
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": namespace,
                    "labels": {"app.kubernetes.io/name": "codehub", "codehub.local/deployment": self.deployment_owner},
                },
            }
        )
        self._record("Namespace", "", namespace, result.metadata.uid)

    def _create_credentials(self, namespace: str) -> None:
        assert self._credentials is not None
        values = dict(self._credentials)
        values["broker-url"] = (
            f"amqp://codehub:{values['broker-password']}@queue.{namespace}.svc.cluster.local:5672/%2F"
        )
        result = self._client().core_v1_api.create_namespaced_secret(
            namespace,
            body={
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "service-credentials", "namespace": namespace},
                "type": "Opaque",
                "data": {name: base64.b64encode(value.encode()).decode() for name, value in values.items()},
            },
        )
        self._record("Secret", namespace, "service-credentials", result.metadata.uid)

    def _create_gateway_tls(self, namespace: str) -> None:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

        now = datetime.datetime.now(datetime.UTC)
        ca_key = ec.generate_private_key(ec.SECP256R1())
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"CodeHub {namespace} CA")])
        authority = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=14))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(ca_key, hashes.SHA256())
        )
        key = ec.generate_private_key(ec.SECP256R1())
        name = f"gateway.{namespace}.svc.cluster.local"
        certificate = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=7))
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName(name),
                        x509.DNSName("gateway"),
                        x509.IPAddress(ipaddress.ip_address("172.17.0.1")),
                        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    ]
                ),
                critical=False,
            )
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        values = {
            "tls.crt": certificate.public_bytes(serialization.Encoding.PEM),
            "tls.key": key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            ),
            "ca.crt": authority.public_bytes(serialization.Encoding.PEM),
        }
        result = self._client().core_v1_api.create_namespaced_secret(
            namespace,
            body={
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "gateway-tls", "namespace": namespace},
                "type": "kubernetes.io/tls",
                "data": {name: base64.b64encode(value).decode() for name, value in values.items()},
            },
        )
        self._record("Secret", namespace, "gateway-tls", result.metadata.uid)
        self.gateway_certificates[namespace] = values["ca.crt"].decode()

    def _capture_namespaced_resources(self, namespace: str) -> None:
        resources = json.loads(
            self._client().exec_command_checked(
                f"kubectl -n {shlex.quote(namespace)} get deployment,statefulset,pod,service,pvc,job,secret,configmap,networkpolicy,endpointslice -o json",
                timeout=30,
            )
        )
        for resource in resources["items"]:
            metadata = resource["metadata"]
            self._record(resource["kind"], namespace, metadata["name"], metadata["uid"])

    def deploy(self):
        if self.inventory.phase != LifecyclePhase.CREATED:
            raise RuntimeError("Create a new application owner for each deployment attempt")
        if not self.chart_path.is_dir():
            raise FileNotFoundError(f"Application chart not found: {self.chart_path}")
        self._validate_boundary()
        client = self._client()
        nodes = client.core_v1_api.list_node().items
        node_labels = {
            node.metadata.name: node.metadata.labels or {}
            for node in nodes
            if not any(
                key in (node.metadata.labels or {})
                for key in ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master")
            )
            and any(
                condition.type == "Ready" and condition.status == "True" for condition in (node.status.conditions or [])
            )
        }
        self.regions = regional_inventory(self.tier, node_labels)
        self.database_groups = mysql_groups(self.tier, self.regions)
        self.inventory = self.inventory.transition(LifecyclePhase.PROVISIONING)
        self._credentials = {
            key: secrets.token_hex(16 if key == "replication-password" else 24)
            for key in (
                "mysql-password",
                "mysql-root-password",
                "mysql-observer-password",
                "replication-password",
                "service-token",
                "bootstrap-token",
                "broker-password",
                "broker-cookie",
            )
        }
        try:
            for region in self.regions:
                self._create_namespace(region.namespace)
                self._create_credentials(region.namespace)
                self._create_gateway_tls(region.namespace)
                values = region_values(self.tier, region, self.database_groups)
                values["mysql"]["writerRoutes"] = [
                    {
                        "name": f"mysql-g{index}-writer-route",
                        "host": next(member.origin for member in group.members if member.role == "writer")
                        .removeprefix("mysql://")
                        .rsplit(":", 1)[0],
                    }
                    for index, group in enumerate(self.database_groups)
                ]
                values["databaseRoutes"] = {"*": self._normal_group_route(region, self.database_groups[0])}
                values["workerDatabaseRoutes"] = dict(values["databaseRoutes"])
                values["databaseLinks"] = [
                    {"name": self._database_link_name(target, group), "host": "172.17.0.1", "port": port}
                    for (source, target, group), port in sorted(self.database_links.items())
                    if source == region.name
                ]
                values["image"] = {"repository": self.image, "pullPolicy": "IfNotPresent"}
                if self.storage_class is not None:
                    values["storageClass"] = self.storage_class
                values["topology"] = {"automaticPromotion": region.name == "region-b"}
                values["deploymentOwner"] = self.deployment_owner
                values["webhookHosts"] = list(self.webhook_hosts)
                values["webhookEgress"] = [{"cidr": cidr, "port": port} for cidr, port in self.webhook_egress]
                with tempfile.TemporaryDirectory(prefix="codehub-values-") as directory:
                    values_path = Path(directory) / "values.yaml"
                    values_path.write_text(yaml.safe_dump(values), encoding="utf-8")
                    client.exec_command_checked(
                        f"helm upgrade --install codehub {shlex.quote(str(self.chart_path))} -n {region.namespace} "
                        f"-f {shlex.quote(str(values_path))} --timeout {self.readiness_seconds}s",
                        timeout=self.readiness_seconds + 30,
                    )
                self._capture_namespaced_resources(region.namespace)
            self._initialize_replication()
            self._migrate()
            self._await_replication()
            for namespace in self.namespaces:
                client.exec_command_checked(
                    f"kubectl -n {namespace} rollout status deployment --timeout={self.readiness_seconds}s",
                    timeout=self.readiness_seconds + 30,
                )
                client.exec_command_checked(
                    f"kubectl -n {namespace} rollout status statefulset --timeout={self.readiness_seconds}s",
                    timeout=self.readiness_seconds + 30,
                )
                self._capture_namespaced_resources(namespace)
            self._await_queue_membership()
            self.inventory = self.inventory.transition(LifecyclePhase.HEALTHY)
        except BaseException:
            self.inventory = self.inventory.transition(LifecyclePhase.INVALID)
            try:
                self.cleanup()
            except Exception:
                self.logger.exception("Owned application deployment cleanup failed")
            raise

    def mysql_command(self, member, sql: str, *, timeout: float = 30) -> str:
        """Execute normal DBA SQL against an exact declared member with a mounted credential."""
        if not any(member in group.members for group in self.database_groups):
            raise ValueError("Database member is outside the captured deployment")
        if type(timeout) not in {int, float} or not 0 < timeout <= self.readiness_seconds + 30:
            raise ValueError("Normal DBA execution requires a bounded positive deadline")
        namespace = next(region.namespace for region in self.regions if region.name == member.region)
        pod = member.origin.removeprefix("mysql://").split(".", 1)[0] + "-0"
        command = f"kubectl -n {namespace} exec -i {pod} -c mysql -- sh -c " + shlex.quote(
            'MYSQL_PWD="$(cat /run/credentials/mysql-root-password)" exec mysql -uroot --batch --raw'
        )
        return self._client().exec_command_checked(command, input_data=sql, timeout=timeout)

    def port_forward_command(
        self, region: str, role: str, local_port: int, *, address: str = "172.17.0.1", tls: bool = False
    ) -> tuple[str, ...]:
        if region not in {item.name for item in self.regions}:
            raise ValueError("Unknown captured region")
        if role not in {"gateway", "api", "repository", "search", "delivery", "topology"}:
            raise ValueError("Unsupported HTTP service")
        if type(local_port) is not int or not 1 <= local_port <= 65535:
            raise ValueError("Invalid forwarding port")
        ipaddress.ip_address(address)
        if tls and role != "gateway":
            raise ValueError("TLS is terminated at the regional gateway")
        namespace = next(item.namespace for item in self.regions if item.name == region)
        return (
            "kubectl",
            "-n",
            namespace,
            "port-forward",
            f"service/{role}",
            f"{local_port}:{8443 if tls else 8080}",
            "--address",
            address,
        )

    def set_writer_route(self, region: str, member) -> None:
        group = next((group for group in self.database_groups if member in group.members), None)
        if group is None:
            raise ValueError("Writer route must target a declared database member")
        host, _ = self.normal_connection_endpoint(region, member)
        self._set_writer_endpoint(region, group, host)

    def _set_writer_endpoint(self, region, group, host):
        namespace = next(item.namespace for item in self.regions if item.name == region)
        index = self.database_groups.index(group)
        names = [f"mysql-g{index}-writer-route", *(["mysql-writer"] if index == 0 else [])]
        core = self._client().core_v1_api
        for name in names:
            self._assert_owned_namespace(namespace)
            resource = self._owned_resource("Service", namespace, name)
            current = core.read_namespaced_service(name, namespace, _request_timeout=5)
            if current.metadata.uid != resource.uid:
                raise RuntimeError("Writer routing service ownership changed")
            core.patch_namespaced_service(
                name,
                namespace,
                body={
                    "metadata": {"uid": resource.uid, "resourceVersion": current.metadata.resource_version},
                    "spec": {"type": "ExternalName", "externalName": host},
                },
                _request_timeout=5,
            )

    def _validate_database_links(self, links):
        if not links:
            return
        regions = {f"region-{chr(97 + i)}" for i in range(self.tier.regions)}
        expected = {
            (source, target, f"group-{group}")
            for group in range(self.tier.database_groups)
            for source in regions
            for target in ("region-a", "region-b")
            if source != target
        }
        if set(links) != expected:
            raise ValueError("Database links must cover every declared remote writer and candidate")
        if any(type(port) is not int or not 1024 <= port <= 32767 for port in links.values()):
            raise ValueError("Database data ports must be explicit bounded nonprivileged integers")
        if len(set(links.values())) != len(links):
            raise ValueError("Database link data ports must be distinct")

    @staticmethod
    def _database_link_name(target, group):
        return f"mysql-link-g{group.removeprefix('group-')}-to-{target}"

    def normal_connection_endpoint(self, source_region, target_member):
        group = next((group for group in self.database_groups if target_member in group.members), None)
        source = next((region for region in self.regions if region.name == source_region), None)
        if group is None or source is None:
            raise ValueError("Database connectivity must use declared regions and members")
        if self.database_links and source_region != target_member.region:
            key = (source_region, target_member.region, group.name)
            if target_member.role not in {"writer", "candidate"} or key not in self.database_links:
                raise ValueError("Remote database member has no declared connection path")
            if not self._database_links_installed:
                raise RuntimeError("Declared database connections have not been installed")
            return (
                f"{self._database_link_name(target_member.region, group.name)}.{source.namespace}.svc.cluster.local",
                3306,
            )
        return target_member.origin.removeprefix("mysql://").rsplit(":", 1)[0], 3306

    def install_database_links(self, endpoints, *, runner_host="172.17.0.1"):
        """Install predeclared normal SQL connectivity before customer provisioning."""
        from kubernetes.client import AppsV1Api, DiscoveryV1Api, NetworkingV1Api

        if self.inventory.phase != LifecyclePhase.HEALTHY or self._database_links_installed:
            raise RuntimeError("Normal database connections install once on a healthy deployment")
        if runner_host != "172.17.0.1" or not self.database_links or dict(endpoints) != self.database_links:
            raise ValueError("SQL endpoints must exactly match the predeclared ordinary data paths")
        core = self._client().core_v1_api
        discovery, networking, apps = (
            factory(core.api_client) for factory in (DiscoveryV1Api, NetworkingV1Api, AppsV1Api)
        )
        pending = []
        for region in self.regions:
            self._assert_owned_namespace(region.namespace)
            policy_owner = self._owned_resource("NetworkPolicy", region.namespace, "database-link-egress")
            policy = networking.read_namespaced_network_policy(
                "database-link-egress", region.namespace, _request_timeout=5
            )
            ports = {port for (source, _, _), port in endpoints.items() if source == region.name}
            rules = policy.spec.egress or []
            if (
                policy.metadata.uid != policy_owner.uid
                or len(rules) != 1
                or len(rules[0].to or []) != 1
                or rules[0].to[0].ip_block is None
                or rules[0].to[0].ip_block.cidr != f"{runner_host}/32"
                or rules[0].to[0].ip_block._except
                or {p.port for p in rules[0].ports or [] if p.protocol == "TCP"} != ports
                or len(rules[0].ports or []) != len(ports)
            ):
                raise RuntimeError("Declared SQL policy differs from its exact owned data ports")
            for (source, target, group), port in sorted(endpoints.items()):
                if source != region.name:
                    continue
                name = self._database_link_name(target, group)
                service_owner = self._owned_resource("Service", region.namespace, name)
                slice_owner = self._owned_resource("EndpointSlice", region.namespace, name)
                service = core.read_namespaced_service(name, region.namespace, _request_timeout=5)
                current = self._read_database_link_slice(discovery, region.namespace, name, slice_owner.uid, port)
                if (
                    service.metadata.uid != service_owner.uid
                    or service.spec.type != "ClusterIP"
                    or service.spec.selector
                    or len(service.spec.ports) != 1
                    or service.spec.ports[0].port != 3306
                ):
                    raise RuntimeError("Predeclared SQL frontend ownership or configuration changed")
                pending.append((region.namespace, name, service_owner.uid, current, port))
        # All declarations are checked before the first mutation. A failed patch
        # leaves the run invalid; its captured namespace remains the cleanup owner.
        for namespace, name, service_uid, before, port in pending:
            self._assert_owned_namespace(namespace)
            if core.read_namespaced_service(name, namespace, _request_timeout=5).metadata.uid != service_uid:
                raise RuntimeError("SQL frontend service was replaced before endpoint installation")
            discovery.patch_namespaced_endpoint_slice(
                name,
                namespace,
                body={
                    "metadata": {
                        "uid": before["metadata"]["uid"],
                        "resourceVersion": before["metadata"]["resourceVersion"],
                        "ownerReferences": [{"apiVersion": "v1", "kind": "Service", "name": name, "uid": service_uid}],
                    },
                    "ports": [{"name": "mysql", "protocol": "TCP", "port": port}],
                    "endpoints": [{"addresses": [runner_host], "conditions": {"ready": True}}],
                },
                _request_timeout=5,
            )
        self._database_links_installed = True
        for region in self.regions:
            for group in self.database_groups:
                writer = next(member for member in group.members if member.role == "writer")
                self.set_writer_route(region.name, writer)
            self._configure_topology_links(region, apps)
        for group in self.database_groups:
            writer = next(member for member in group.members if member.role == "writer")
            for member in group.members:
                if member.region != writer.region:
                    host, port = self.normal_connection_endpoint(member.region, writer)
                    self.mysql_command(
                        member,
                        f"STOP REPLICA; CHANGE REPLICATION SOURCE TO SOURCE_HOST='{host}', SOURCE_PORT={port}; START REPLICA;",
                    )
        self._await_replication()
        return {
            key: f"{self._database_link_name(key[1], key[2])}.codehub-{key[0]}.svc.cluster.local" for key in endpoints
        }

    @staticmethod
    def _read_database_link_slice(discovery, namespace, name, owner_uid, port):
        # Kubernetes may omit the empty endpoints field. The pinned generated
        # model rejects that omission, so read this one response without model
        # construction and validate its normal declaration explicitly.
        response = discovery.read_namespaced_endpoint_slice(name, namespace, _preload_content=False, _request_timeout=5)
        try:
            if response.status != 200:
                raise RuntimeError("SQL endpoint declaration could not be read")
            payload = response.read(DATABASE_LINK_SLICE_MAX_BYTES + 1)
            if type(payload) is not bytes or len(payload) > DATABASE_LINK_SLICE_MAX_BYTES:
                raise RuntimeError("SQL endpoint declaration exceeds its bounded response size")
            try:
                current = json.loads(payload)
            except (ValueError, UnicodeError, RecursionError) as error:
                raise RuntimeError("SQL endpoint declaration is not valid JSON") from error
        finally:
            try:
                response.close()
            finally:
                response.release_conn()
        if type(current) is not dict:
            raise RuntimeError("SQL endpoint declaration must be a JSON object")
        metadata = current.get("metadata")
        if type(metadata) is not dict:
            raise RuntimeError("Predeclared SQL endpoint field metadata missing or mismatched")
        labels = metadata.get("labels")
        resource_version = metadata.get("resourceVersion")
        endpoints = current.get("endpoints", [])
        # An empty native endpoint list may round-trip through API storage as
        # null. Only null and omission share the semantics of an empty list.
        if endpoints is None:
            endpoints = []
        ports = current.get("ports")
        checks = (
            ("apiVersion", current.get("apiVersion") == "discovery.k8s.io/v1"),
            ("kind", current.get("kind") == "EndpointSlice"),
            ("metadata.name", metadata.get("name") == name),
            ("metadata.namespace", metadata.get("namespace") == namespace),
            ("metadata.uid", metadata.get("uid") == owner_uid),
            ("metadata.resourceVersion", type(resource_version) is str and 1 <= len(resource_version) <= 1024),
            ("metadata.labels", type(labels) is dict),
            (
                "metadata.labels.kubernetes.io/service-name",
                type(labels) is dict and labels.get("kubernetes.io/service-name") == name,
            ),
            ("addressType", current.get("addressType") == "IPv4"),
            ("endpoints", type(endpoints) is list and not endpoints),
            ("ports", type(ports) is list and len(ports) == 1 and type(ports[0]) is dict),
        )
        for field, valid in checks:
            if not valid:
                raise RuntimeError(f"Predeclared SQL endpoint field {field} missing or mismatched")
        for field, valid in (
            ("ports[0].name", ports[0].get("name") == "mysql"),
            ("ports[0].protocol", ports[0].get("protocol") == "TCP"),
            ("ports[0].port", type(ports[0].get("port")) is int and ports[0]["port"] == port),
        ):
            if not valid:
                raise RuntimeError(f"Predeclared SQL endpoint field {field} missing or mismatched")
        return current

    def _configure_topology_links(self, region, apps):
        group = self.database_groups[0]
        writer = next(member for member in group.members if member.role == "writer")
        candidate = next(member for member in group.members if member.role == "candidate")
        self._assert_owned_namespace(region.namespace)
        owner = self._owned_resource("ConfigMap", region.namespace, "topology-config")
        core = self._client().core_v1_api
        current = core.read_namespaced_config_map("topology-config", region.namespace, _request_timeout=5)
        if current.metadata.uid != owner.uid:
            raise RuntimeError("Topology configuration ownership changed")
        configuration = json.loads(current.data["topology.json"])
        configuration.update(
            writer_host=self.normal_connection_endpoint(region.name, writer)[0],
            candidate_host=self.normal_connection_endpoint(region.name, candidate)[0],
            routing_service="mysql-g0-writer-route",
        )
        encoded = json.dumps(configuration, sort_keys=True, separators=(",", ":"))
        core.patch_namespaced_config_map(
            "topology-config",
            region.namespace,
            body={
                "metadata": {"uid": owner.uid, "resourceVersion": current.metadata.resource_version},
                "data": dict(current.data) | {"topology.json": encoded},
            },
            _request_timeout=5,
        )
        deployment_owner = self._owned_resource("Deployment", region.namespace, "topology")
        deployment = apps.read_namespaced_deployment("topology", region.namespace, _request_timeout=5)
        if deployment.metadata.uid != deployment_owner.uid:
            raise RuntimeError("Topology deployment ownership changed")
        self._assert_owned_namespace(region.namespace)
        apps.patch_namespaced_deployment(
            "topology",
            region.namespace,
            body={
                "metadata": {"uid": deployment_owner.uid, "resourceVersion": deployment.metadata.resource_version},
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {"codehub.local/config-sha256": hashlib.sha256(encoded.encode()).hexdigest()}
                        }
                    }
                },
            },
            _request_timeout=5,
        )
        self._client().exec_command_checked(
            f"kubectl -n {region.namespace} rollout status deployment/topology --timeout={self.readiness_seconds}s",
            timeout=self.readiness_seconds + 5,
        )
        self._await_topology_configuration(region, encoded)

    def _await_topology_configuration(self, region, encoded):
        expected = hashlib.sha256(encoded.encode()).hexdigest()
        program = "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('/etc/codehub/topology.json').read_bytes()).hexdigest())"
        deadline = time.monotonic() + self.readiness_seconds
        while time.monotonic() < deadline:
            pods = self._serving_route_pods(region, "topology")
            self._assert_owned_namespace(region.namespace)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            output = self._client().exec_command_checked(
                f"kubectl -n {region.namespace} exec {pods[0]} -- python -c {shlex.quote(program)}",
                timeout=min(5, remaining),
            )
            if output.strip() == expected:
                return
            time.sleep(0.2)
        raise TimeoutError("Current topology replica did not load its declared database connections")

    def _owned_resource(self, kind, namespace, name):
        resource = next(
            (
                item
                for item in self.inventory.resources
                if item.kind == kind and item.namespace == namespace and item.name == name
            ),
            None,
        )
        if resource is None:
            raise RuntimeError("Normal routing resource was not captured as owned")
        return resource

    def _assert_owned_namespace(self, namespace):
        resource = self._owned_resource("Namespace", "", namespace)
        current = self._client().core_v1_api.read_namespace(namespace, _request_timeout=5)
        if (
            current.metadata.uid != resource.uid
            or (current.metadata.labels or {}).get("codehub.local/deployment") != self.deployment_owner
        ):
            raise RuntimeError("Normal routing namespace ownership changed")

    def _owned_route_map(self, namespace, name):
        self._assert_owned_namespace(namespace)
        resource = self._owned_resource("ConfigMap", namespace, name)
        current = self._client().core_v1_api.read_namespaced_config_map(name, namespace, _request_timeout=5)
        if current.metadata.uid != resource.uid:
            raise RuntimeError("Normal route ConfigMap ownership changed")
        if not current.data or "database-routes.json" not in current.data:
            raise RuntimeError("Normal route ConfigMap lacks its declared file")
        routes = json.loads(current.data["database-routes.json"])
        if not isinstance(routes, dict):
            raise ValueError("Normal database routes must be a JSON object")
        return current, routes

    def _normal_group_route(self, region, group):
        index = self.database_groups.index(group)
        reader = next(member for member in group.members if member.region == region.name and member.role == "reader")
        return {
            "group": group.name,
            "writer_host": f"mysql-g{index}-writer-route.{region.namespace}.svc.cluster.local",
            "reader_host": reader.origin.removeprefix("mysql://").rsplit(":", 1)[0],
            "port": 3306,
            "read_port": 3306,
            "database": "codehub",
            "user": "codehub",
            "password_file": "/run/credentials/mysql-password",
        }

    def configure_tenant_route(self, tenant_id: str, group: str) -> None:
        """Install ordinary tenant routing in both maps before identity provisioning."""
        if type(tenant_id) is not str or str(UUID(tenant_id)) != tenant_id:
            raise ValueError("Tenant routing requires a canonical identity")
        target = next((item for item in self.database_groups if item.name == group), None)
        if target is None:
            raise ValueError("Tenant routing requires a declared database group")
        plan = []
        for region in self.regions:
            route = self._normal_group_route(region, target)
            for name in ("database-routes", "worker-database-routes"):
                current, routes = self._owned_route_map(region.namespace, name)
                if tenant_id in routes and (
                    not isinstance(routes[tenant_id], dict) or routes[tenant_id].get("group") != group
                ):
                    raise ValueError("Normal tenant assignment cannot silently change its database group")
                routes[tenant_id] = route
                data = dict(current.data) | {
                    "database-routes.json": json.dumps(routes, sort_keys=True, separators=(",", ":"))
                }
                plan.append((region.namespace, name, current, data))
        applied = []
        core = self._client().core_v1_api
        try:
            for namespace, name, before, data in plan:
                self._assert_owned_namespace(namespace)
                after = core.patch_namespaced_config_map(
                    name,
                    namespace,
                    body={
                        "metadata": {"uid": before.metadata.uid, "resourceVersion": before.metadata.resource_version},
                        "data": data,
                    },
                    _request_timeout=5,
                )
                applied.append((namespace, name, before, after, data))
        except Exception as original:
            failures = [original]
            for namespace, name, before, after, data in reversed(applied):
                try:
                    current, _ = self._owned_route_map(namespace, name)
                    if current.metadata.resource_version != after.metadata.resource_version or current.data != data:
                        raise RuntimeError("Concurrent route change prevents safe rollback")
                    core.patch_namespaced_config_map(
                        name,
                        namespace,
                        body={
                            "metadata": {
                                "uid": before.metadata.uid,
                                "resourceVersion": current.metadata.resource_version,
                            },
                            "data": before.data,
                        },
                        _request_timeout=5,
                    )
                except Exception as rollback_error:
                    failures.append(rollback_error)
            if len(failures) > 1:
                raise ExceptionGroup("Normal routing update and guarded rollback failed", failures) from original
            raise

    def _serving_route_pods(self, region, role):
        from kubernetes.client import AppsV1Api

        self._assert_owned_namespace(region.namespace)
        owner = self._owned_resource("StatefulSet" if role == "repository" else "Deployment", region.namespace, role)
        client = self._client().core_v1_api
        apps = AppsV1Api(client.api_client)
        pods = client.list_namespaced_pod(
            region.namespace, label_selector=f"app.kubernetes.io/component={role}", _request_timeout=5
        ).items
        result = []
        for pod in pods:
            if pod.metadata.deletion_timestamp or not any(
                condition.type == "Ready" and condition.status == "True" for condition in pod.status.conditions or ()
            ):
                continue
            refs = [ref for ref in pod.metadata.owner_references or () if ref.controller]
            if len(refs) != 1:
                raise RuntimeError("Serving route pod has ambiguous controller ownership")
            ref = refs[0]
            if owner.kind == "StatefulSet":
                matches = ref.kind == owner.kind and ref.uid == owner.uid and ref.name == owner.name
            else:
                replica = apps.read_namespaced_replica_set(ref.name, region.namespace, _request_timeout=5)
                matches = (
                    ref.kind == "ReplicaSet"
                    and replica.metadata.uid == ref.uid
                    and any(
                        parent.controller
                        and parent.kind == owner.kind
                        and parent.name == owner.name
                        and parent.uid == owner.uid
                        for parent in replica.metadata.owner_references or ()
                    )
                )
            if not matches:
                raise RuntimeError("Serving route pod differs from its captured controller")
            result.append(pod.metadata.name)
        expected = (
            1
            if role in {"repository", "topology"}
            else getattr(self.tier, f"{'workers' if role == 'worker' else 'api'}_per_zone")
        )
        if len(result) != expected:
            raise RuntimeError("Serving normal route replicas are incomplete")
        return tuple(result)

    def await_database_routes(self, tenant_groups: dict[str, str], *, timeout_seconds: int = 120) -> None:
        """Prove normal ConfigMap propagation before the first customer identity."""
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300 or not tenant_groups:
            raise ValueError("Normal route propagation needs bounded time and declared tenants")
        deadline = time.monotonic() + timeout_seconds
        program = "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('/etc/codehub/database-routes.json').read_bytes()).hexdigest())"
        while time.monotonic() < deadline:
            complete = True
            for region in self.regions:
                for role in ("api", "repository", "worker"):
                    name = "worker-database-routes" if role == "worker" else "database-routes"
                    current, routes = self._owned_route_map(region.namespace, name)
                    if any(
                        not isinstance(routes.get(tenant), dict) or routes[tenant].get("group") != group
                        for tenant, group in tenant_groups.items()
                    ):
                        raise RuntimeError("Normal configuration omits declared tenant routing")
                    expected = hashlib.sha256(current.data["database-routes.json"].encode()).hexdigest()
                    for pod in self._serving_route_pods(region, role):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("Normal database routes did not propagate to every serving replica")
                        self._assert_owned_namespace(region.namespace)
                        output = self._client().exec_command_checked(
                            f"kubectl -n {region.namespace} exec {pod} -- python -c {shlex.quote(program)}",
                            timeout=min(5, remaining),
                        )
                        complete &= output.strip() == expected
            if complete:
                return
            time.sleep(0.2)
        raise TimeoutError("Normal database routes did not propagate to every serving replica")

    @property
    def observer_password(self) -> str:
        if self._credentials is None:
            raise RuntimeError("Application credentials are not available")
        return self._credentials["mysql-observer-password"]

    def _mysql_json(self, member, sql, *, timeout=30):
        rows = [
            json.loads(line)
            for line in self.mysql_command(member, sql, timeout=timeout).splitlines()
            if line.startswith("{")
        ]
        if len(rows) != 1 or type(rows[0]) is not dict:
            raise RuntimeError("Normal database metadata query returned no unique observation")
        return rows[0]

    def _assert_bootstrap_member(self, member):
        if self.inventory.phase != LifecyclePhase.PROVISIONING:
            raise RuntimeError("Physical database bootstrap is restricted to a fresh provisioning owner")
        namespace = next(region.namespace for region in self.regions if region.name == member.region)
        name = member.origin.removeprefix("mysql://").split(".", 1)[0]
        self._assert_owned_namespace(namespace)
        owned = self._owned_resource("StatefulSet", namespace, name)
        actual = json.loads(
            self._client().exec_command_checked(
                f"kubectl -n {namespace} get statefulset/{name} pod/{name}-0 -o json", timeout=10
            )
        )
        resources = {item.get("kind"): item for item in actual.get("items", ())}
        stateful, pod = resources.get("StatefulSet", {}), resources.get("Pod", {})
        if stateful.get("metadata", {}).get("uid") != owned.uid or not any(
            reference.get("kind") == "StatefulSet"
            and reference.get("uid") == owned.uid
            and reference.get("controller") is True
            for reference in pod.get("metadata", {}).get("ownerReferences", ())
        ):
            raise RuntimeError("Fresh database bootstrap controller ownership changed")
        for spec in (stateful.get("spec", {}).get("template", {}).get("spec", {}), pod.get("spec", {})):
            images = [
                container.get("image") for container in spec.get("containers", ()) if container.get("name") == "mysql"
            ]
            if images != [MYSQL_BOOTSTRAP_IMAGE]:
                raise RuntimeError("Physical database bootstrap requires the pinned MySQL image")
        return namespace, name

    def _fresh_database_metadata(self, member):
        info = self._mysql_json(
            member,
            "SELECT JSON_OBJECT('tables',(SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='codehub'),"
            "'channels',(SELECT COUNT(*) FROM performance_schema.replication_connection_configuration),"
            "'uuid',@@GLOBAL.server_uuid,'read_only',@@GLOBAL.read_only,'version',@@version,"
            "'gtid_mode',@@GLOBAL.gtid_mode,'log_bin',@@GLOBAL.log_bin);",
        )
        if info.get("tables") != 0 or info.get("channels") != 0:
            raise RuntimeError(
                "Fresh physical bootstrap cannot replace application tables or existing replication channels"
            )
        if (
            info.get("version") != "8.4.6"
            or info.get("gtid_mode") != "ON"
            or info.get("log_bin") != 1
            or info.get("read_only") != int(member.role != "writer")
        ):
            raise RuntimeError("Fresh database configuration differs from the declared MySQL replication roles")
        if type(info.get("uuid")) is not str or str(UUID(info["uuid"])) != info["uuid"]:
            raise RuntimeError("Fresh database has no canonical server identity")
        return info

    def _preflight_replication_bootstrap(self):
        if self.inventory.phase != LifecyclePhase.PROVISIONING:
            raise RuntimeError("Physical database bootstrap is restricted to a fresh provisioning owner")
        metadata, identities = {}, set()
        # Check every group before changing any member, including additional readers.
        for group in self.database_groups:
            for member in group.members:
                namespace, name = self._assert_bootstrap_member(member)
                self._client().exec_command_checked(
                    f"kubectl -n {namespace} wait pod/{name}-0 --for=condition=Ready --timeout={self.readiness_seconds}s",
                    timeout=self.readiness_seconds + 30,
                )
                info = self._fresh_database_metadata(member)
                if info["uuid"] in identities:
                    raise RuntimeError("Fresh database members cannot share server identities")
                identities.add(info["uuid"])
                metadata[member.name] = info
                library = (
                    'plugin=$(MYSQL_PWD="$(cat /run/credentials/mysql-root-password)" '
                    'mysql -uroot --batch --raw --skip-column-names -e "SELECT @@plugin_dir"); '
                    'test -n "$plugin" && test -r "${plugin}/mysql_clone.so"'
                )
                self._client().exec_command_checked(
                    f"kubectl -n {namespace} exec {name}-0 -c mysql -- sh -c {shlex.quote(library)}", timeout=10
                )
        return metadata

    def _clone_initial_replica(self, writer, member, credential, required_gtid, server_uuid):
        self._assert_bootstrap_member(member)
        self._fresh_database_metadata(member)
        previous = self._mysql_json(
            member, "SELECT JSON_OBJECT('previous',COUNT(*)) FROM performance_schema.clone_status;"
        )
        if previous.get("previous") != 0:
            raise RuntimeError("Fresh replica bootstrap cannot adopt an earlier clone operation")
        source = writer.origin.removeprefix("mysql://").rsplit(":", 1)[0]
        self.mysql_command(member, f"SET GLOBAL clone_valid_donor_list='{source}:3306';")
        deadline = time.monotonic() + self.readiness_seconds
        command_error = None
        try:
            self.mysql_command(
                member,
                f"CLONE INSTANCE FROM 'snapshot_copy'@'{source}':3306 IDENTIFIED BY '{credential}';",
                timeout=max(0.01, deadline - time.monotonic()),
            )
        except Exception as exc:
            # A standalone mysqld may report 3707 after copying and shutting down.
            # No CLI error is treated as success without the post-restart proof.
            command_error = exc
        while time.monotonic() < deadline:
            self._assert_bootstrap_member(member)
            remaining = deadline - time.monotonic()
            try:
                status = self._mysql_json(
                    member,
                    "SELECT JSON_OBJECT('state',STATE,'source',SOURCE,'error',ERROR_NO,'gtid',GTID_EXECUTED,"
                    f"'covered',GTID_SUBSET('{required_gtid}',GTID_EXECUTED),"
                    f"'applied',GTID_SUBSET('{required_gtid}',@@GLOBAL.gtid_executed),"
                    "'uuid',@@GLOBAL.server_uuid) FROM performance_schema.clone_status;",
                    timeout=max(0.01, min(5, remaining)),
                )
            except Exception as exc:
                if command_error is None:
                    command_error = exc
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
                continue
            if status.get("state") == "Completed":
                if (
                    status.get("source") != f"{source}:3306"
                    or status.get("error") != 0
                    or status.get("covered") != 1
                    or status.get("applied") != 1
                    or status.get("uuid") != server_uuid
                ):
                    raise RuntimeError("Physical replica snapshot has invalid donor, GTID coverage, or server identity")
                return
            if status.get("state") != "In Progress":
                raise RuntimeError("Physical replica snapshot did not complete successfully") from command_error
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        raise TimeoutError("Physical replica did not restart with a proven complete snapshot") from command_error

    def _replication_diagnostics(self, member, *, extra_secrets=()):
        try:
            observed = self._mysql_json(
                member,
                "SELECT JSON_OBJECT('connection_errors',(SELECT JSON_ARRAYAGG(JSON_OBJECT('number',LAST_ERROR_NUMBER,"
                "'message',LAST_ERROR_MESSAGE)) FROM performance_schema.replication_connection_status WHERE LAST_ERROR_NUMBER<>0),"
                "'last_sql_error',(SELECT JSON_ARRAYAGG(JSON_OBJECT('number',LAST_ERROR_NUMBER,'message',LAST_ERROR_MESSAGE)) "
                "FROM performance_schema.replication_applier_status_by_worker WHERE LAST_ERROR_NUMBER<>0),"
                "'clone_errors',(SELECT JSON_ARRAYAGG(JSON_OBJECT('number',ERROR_NO,'message',ERROR_MESSAGE)) "
                "FROM performance_schema.clone_status WHERE ERROR_NO<>0));",
                timeout=5,
            )
            text = json.dumps(observed, sort_keys=True)
            for secret in sorted((*((self._credentials or {}).values()), *extra_secrets), key=len, reverse=True):
                if secret:
                    text = text.replace(secret, "[redacted]")
            text = re.sub(r"(?i)(IDENTIFIED\s+(?:BY|WITH\s+'[^']+'\s+AS)\s*)'[^']*'", r"\1'[redacted]'", text)
            self.logger.error("Database replication failed for %s: %s", member.name, text[:8192])
            return text[:8192]
        except Exception:
            self.logger.error("Database replication diagnostics unavailable for %s", member.name)
            return "unavailable"

    def _initialize_replication(self) -> None:
        metadata = self._preflight_replication_bootstrap()
        for group in self.database_groups:
            writer = next(member for member in group.members if member.role == "writer")
            assert self._credentials is not None
            password = self._credentials["replication-password"]
            for member in group.members:
                self.mysql_command(
                    member,
                    "SET SESSION sql_log_bin=0; INSTALL PLUGIN clone SONAME 'mysql_clone.so'; "
                    "GRANT CLONE_ADMIN ON *.* TO 'root'@'localhost'; "
                    f"CREATE USER IF NOT EXISTS 'replication'@'%' IDENTIFIED BY '{password}'; "
                    "GRANT REPLICATION SLAVE ON *.* TO 'replication'@'%'; SET SESSION sql_log_bin=1;",
                )
            observer = self._credentials["mysql-observer-password"]
            self.mysql_command(
                writer,
                f"CREATE USER IF NOT EXISTS 'observer'@'%' IDENTIFIED BY '{observer}'; "
                "GRANT SELECT ON codehub.* TO 'observer'@'%'; GRANT REPLICATION CLIENT ON *.* TO 'observer'@'%';",
            )
            credential = secrets.token_hex(16)
            self.mysql_command(
                writer,
                f"SET SESSION sql_log_bin=0; CREATE USER 'snapshot_copy'@'%' IDENTIFIED BY '{credential}'; "
                "GRANT BACKUP_ADMIN ON *.* TO 'snapshot_copy'@'%'; SET SESSION sql_log_bin=1; "
                "SET GLOBAL super_read_only=ON; SET GLOBAL read_only=ON;",
            )
            required_gtid = self._mysql_json(writer, "SELECT JSON_OBJECT('gtid',@@GLOBAL.gtid_executed);")["gtid"]
            if (
                type(required_gtid) is not str
                or not required_gtid
                or not re.fullmatch(r"[0-9a-fA-F:,\-\n]+", required_gtid)
            ):
                raise RuntimeError("Fresh writer has no valid snapshot GTID history")
            for member in group.members:
                if member == writer:
                    continue
                try:
                    self._clone_initial_replica(
                        writer, member, credential, required_gtid, metadata[member.name]["uuid"]
                    )
                except BaseException:
                    self._replication_diagnostics(member, extra_secrets=(credential,))
                    raise
            for member in group.members:
                self._assert_bootstrap_member(member)
                self.mysql_command(
                    member,
                    "SET GLOBAL super_read_only=OFF; SET SESSION sql_log_bin=0; "
                    "DROP USER 'snapshot_copy'@'%'; SET SESSION sql_log_bin=1;",
                )
            source = writer.origin.removeprefix("mysql://").rsplit(":", 1)[0]
            for member in group.members:
                if member == writer:
                    continue
                try:
                    self.mysql_command(
                        member,
                        f"CHANGE REPLICATION SOURCE TO SOURCE_HOST='{source}', SOURCE_PORT=3306, "
                        f"SOURCE_USER='replication', SOURCE_PASSWORD='{password}', SOURCE_AUTO_POSITION=1, "
                        "GET_SOURCE_PUBLIC_KEY=1, SOURCE_CONNECT_RETRY=1; START REPLICA; SET GLOBAL super_read_only=ON;",
                    )
                except BaseException:
                    self._replication_diagnostics(member)
                    raise
            self.mysql_command(writer, "SET GLOBAL super_read_only=OFF; SET GLOBAL read_only=OFF;")

    def _migrate(self) -> None:
        namespace = self.regions[0].namespace
        for group_index, _ in enumerate(self.database_groups):
            host = f"mysql-g{group_index}-writer.{namespace}.svc.cluster.local"
            self._client().exec_command_checked(
                f"kubectl -n {namespace} exec deployment/api -- env CODEHUB_MYSQL_HOST={host} "
                "python -m codehub.migrate /app/migrations/mysql/001_initial.sql",
                timeout=self.readiness_seconds,
            )

    def _await_replication(self) -> None:
        for group in self.database_groups:
            writer = next(member for member in group.members if member.role == "writer")
            executed = self.mysql_command(writer, "SELECT @@GLOBAL.gtid_executed;").strip().splitlines()[-1]
            if not executed:
                raise RuntimeError("Writer produced no initialized GTID history")
            for member in group.members:
                if member == writer:
                    continue
                try:
                    result = self.mysql_command(member, f"SELECT WAIT_FOR_EXECUTED_GTID_SET('{executed}', 20);")
                except Exception as exc:
                    details = self._replication_diagnostics(member)
                    raise RuntimeError(
                        f"Initial replication observation failed: {member.name}; diagnostics={details}"
                    ) from exc
                if result.strip().splitlines()[-1] != "0":
                    details = self._replication_diagnostics(member)
                    raise RuntimeError(f"Initial replication did not converge: {member.name}; diagnostics={details}")

    def _await_queue_membership(self) -> None:
        for region in self.regions:
            expected = {
                f"rabbit@queue-{index}.queue-headless.{region.namespace}.svc.cluster.local" for index in range(3)
            }
            deadline = time.monotonic() + self.readiness_seconds
            while True:
                clusters = [
                    json.loads(
                        self._client().exec_command_checked(
                            f"kubectl -n {region.namespace} exec queue-{index} -- rabbitmqctl cluster_status --formatter json",
                            timeout=30,
                        )
                    )
                    for index in range(3)
                ]
                if all(
                    set(cluster["running_nodes"]) == expected
                    and not cluster.get("partitions")
                    and not cluster.get("alarms")
                    for cluster in clusters
                ):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Queue has not formed its complete three-node cluster: {region.name}")
                time.sleep(1)

    def start_workload(self):
        # This application's Problem owns traffic and receipts through its hooks.
        raise RuntimeError("CodeHub requires an owned workload controller")

    def cleanup(self):
        if self.inventory.phase == LifecyclePhase.CREATED and not self.inventory.resources:
            return
        if self.inventory.phase == LifecyclePhase.STOPPED:
            return
        self.inventory = self.inventory.transition(LifecyclePhase.STOPPING)
        errors = []
        for resource in self.inventory.cleanup_pending:
            if resource.kind != "Namespace":
                continue
            try:
                try:
                    current = self._client().core_v1_api.read_namespace(resource.name)
                except Exception as exc:
                    if getattr(exc, "status", None) != 404:
                        raise
                    current = None
                if current is not None:
                    if (
                        current.metadata.uid != resource.uid
                        or (current.metadata.labels or {}).get("codehub.local/deployment") != self.deployment_owner
                    ):
                        raise RuntimeError(f"Namespace ownership changed: {resource.name}")
                    self._client().core_v1_api.delete_namespace(
                        resource.name,
                        body={
                            "apiVersion": "v1",
                            "kind": "DeleteOptions",
                            "preconditions": {"uid": resource.uid},
                            "propagationPolicy": "Foreground",
                        },
                    )
                    deadline = time.monotonic() + self.readiness_seconds
                    while True:
                        try:
                            current = self._client().core_v1_api.read_namespace(resource.name)
                        except Exception as exc:
                            if getattr(exc, "status", None) == 404:
                                break
                            raise
                        if current.metadata.uid != resource.uid:
                            raise RuntimeError(f"Namespace replaced during teardown: {resource.name}")
                        if time.monotonic() >= deadline:
                            raise TimeoutError(f"Namespace teardown did not finish: {resource.name}")
                        time.sleep(0.2)
                # Namespace disappearance establishes removal of its scoped objects.
                for child in self.inventory.cleanup_pending:
                    if child == resource or child.namespace == resource.name:
                        self.inventory = self.inventory.mark_removed(child)
            except Exception as exc:
                errors.append(f"{resource.name}: {type(exc).__name__}: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))
        self.inventory = self.inventory.transition(LifecyclePhase.STOPPED)
        self._credentials = None
