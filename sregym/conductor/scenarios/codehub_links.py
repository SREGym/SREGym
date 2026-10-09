"""Private, owned TCP relays for logical regional database connections.

Toxiproxy 2.12.0 implements byte-stream latency, per-connection bandwidth and
TCP reset, not arbitrary IP packet loss. Its released API has no authentication.
The runner must qualify that the rootless workload cannot reach the actual
loopback listener; binding loopback alone is not a security qualification.

Upstream port forwards belong to the scenario controller. This module never
creates a workload mount, changes a firewall, or takes ownership of those forwards.
Sources: https://github.com/Shopify/toxiproxy/tree/v2.12.0 and its tagged
cmd/server/server.go, api.go, Dockerfile, and toxics/bandwidth.go.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps

from docker.errors import NotFound

from sregym.service.docker_runtime import rootless_workload_enabled, trusted_docker_host, validate_rootless_boundary

# Official 2.12.0 manifest list resolved from GHCR; linux/amd64 child is
# sha256:a3e244375123dad8849091bcc59775e188624d3f602db01901f9af855682fef8.
TOXIPROXY_IMAGE = "ghcr.io/shopify/toxiproxy@sha256:9378ed52a28bc50edc1350f936f518f31fa95f0d15917d6eb40b8e376d1a214e"
OWNER_LABEL = "codehub.local/link-owner"
MAX_LINKS = 48
MAX_RESPONSE_BYTES = 1_048_576
_IDENTIFIER = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")


def _port(value: int) -> int:
    if type(value) is not int or not 1024 <= value <= 32767:
        raise ValueError("Ports must be explicit nonprivileged integers outside the default Linux ephemeral range")
    return value


def _ipv4(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value)
    except (ipaddress.AddressValueError, TypeError) as exc:
        raise ValueError("Link addresses must be explicit IPv4 literals") from exc
    if address.is_unspecified or address.is_multicast or address == ipaddress.IPv4Address("255.255.255.255"):
        raise ValueError("Wildcard, multicast and broadcast addresses are forbidden")
    return str(address)


@dataclass(frozen=True, slots=True)
class LinkSpec:
    source_region: str
    target_region: str
    group: str
    public_port: int
    upstream_host: str
    upstream_port: int

    def __post_init__(self):
        for value in (self.source_region, self.target_region, self.group):
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ValueError("Region and group names must be bounded lowercase identifiers")
        if self.source_region == self.target_region:
            raise ValueError("Regional links must join distinct logical regions")
        _port(self.public_port)
        _port(self.upstream_port)
        _ipv4(self.upstream_host)


@dataclass(frozen=True, slots=True)
class LinkProfile:
    """Symmetric stream settings; nominal added RTT is twice latency_ms.

    bandwidth_kbps uses the release's decimal KB/s (1000 bytes/second) and
    limits each connection's two streams separately, not aggregate link capacity.
    None leaves bandwidth/reset absent; reset_timeout_ms=0 resets immediately.
    """

    latency_ms: int = 0
    jitter_ms: int = 0
    bandwidth_kbps: int | None = None
    reset_timeout_ms: int | None = None

    def __post_init__(self):
        for name, maximum in (("latency_ms", 5000), ("jitter_ms", 5000)):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError(f"{name} must be an integer between zero and {maximum}")
        if self.jitter_ms > self.latency_ms:
            raise ValueError("Jitter cannot exceed stream latency")
        if self.bandwidth_kbps is not None and (
            type(self.bandwidth_kbps) is not int or not 1 <= self.bandwidth_kbps <= 64_000
        ):
            raise ValueError("Bandwidth must be between 1 and 64000 decimal KB/s per connection")
        if self.reset_timeout_ms is not None and (
            type(self.reset_timeout_ms) is not int or not 0 <= self.reset_timeout_ms <= 10_000
        ):
            raise ValueError("Reset timeout must be an integer between zero and 10000 milliseconds")


class LinkControlError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise LinkControlError("Link control redirects are forbidden", status=code)


def _docker_client(host: str):
    import docker

    return docker.DockerClient(base_url=host, timeout=10)


def _locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return call


class RegionalLinkController:
    """Lazy private controller; cleanup removes only the captured owned container.

    Host networking is selected explicitly on the trusted engine so the server
    itself binds the requested distinct addresses. Bridge publishing would leave
    the unauthenticated admin API listening on an additional container address.
    There is no host-network fallback and no workload host-network permission.
    Real reachability, timeout, throughput and CPU/memory limits still require
    live qualification before these links can support experimental claims.
    """

    def __init__(
        self,
        *,
        run_id: str,
        trusted_host: str,
        control_bind: str,
        data_bind: str,
        control_port: int,
        readiness_seconds: int = 20,
        client_factory: Callable = _docker_client,
        request: Callable | None = None,
    ):
        self.run_id = str(uuid.UUID(run_id))
        if not isinstance(trusted_host, str) or not trusted_host.startswith("unix:///") or "\x00" in trusted_host:
            raise ValueError("An explicit local trusted Docker Unix socket is required")
        self.trusted_host = trusted_host
        self.control_bind, self.data_bind = _ipv4(control_bind), _ipv4(data_bind)
        if self.control_bind != "127.0.0.1" or ipaddress.IPv4Address(self.data_bind).is_loopback:
            raise ValueError("Control must bind 127.0.0.1 and data must bind a distinct reachable runner address")
        self.control_port = _port(control_port)
        if type(readiness_seconds) is not int or not 1 <= readiness_seconds <= 60:
            raise ValueError("Readiness must be bounded to 1..60 seconds")
        self.readiness_seconds = readiness_seconds
        self._client_factory, self._request_override = client_factory, request
        self._client = self._container = None
        self._container_id = None
        self._owner = uuid.uuid4().hex
        self._name = f"link-relay-{self._owner}"
        self._specs: tuple[LinkSpec, ...] = ()
        self._baseline: dict[LinkSpec, LinkProfile] = {}
        self._toxics: dict[LinkSpec, set[str]] = {}
        self._state = "new"
        self._lock = threading.RLock()

    @_locked
    def prepare(self, specs: tuple[LinkSpec, ...], *, baseline: LinkProfile = LinkProfile()) -> None:
        """Validate configuration without starting Docker or a network connection."""
        if self._state not in {"new", "prepared"}:
            raise LinkControlError("An active or stopped controller cannot be reconfigured")
        if not isinstance(specs, tuple) or not 1 <= len(specs) <= MAX_LINKS:
            raise ValueError(f"Supply a tuple containing 1..{MAX_LINKS} owned links")
        if not isinstance(baseline, LinkProfile):
            raise TypeError("A validated baseline LinkProfile is required")
        ports: set[int] = set()
        for spec in specs:
            if not isinstance(spec, LinkSpec):
                raise TypeError("Every link must be a LinkSpec")
            if spec.public_port in ports or spec.public_port == self.control_port:
                raise ValueError("Each data listener requires a unique port distinct from control")
            ports.add(spec.public_port)
            if spec.upstream_host not in {self.control_bind, self.data_bind}:
                raise ValueError("Upstreams must be explicit runner-owned local port forwards")
            if spec.upstream_port == self.control_port:
                raise ValueError("A data relay cannot target the private control API")
        if ports.intersection(spec.upstream_port for spec in specs):
            raise ValueError("Upstream port forwards must not overlap relay listeners")
        self._specs = specs
        self._baseline = {spec: baseline for spec in specs}
        self._toxics = {spec: set() for spec in specs}
        self._state = "prepared"

    def _check_ports(self):
        for host, port in [
            (self.control_bind, self.control_port),
            *((self.data_bind, s.public_port) for s in self._specs),
        ]:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind((host, port))

    def _owned_container(self):
        if self._container is None or self._container_id is None:
            raise LinkControlError("The owned relay has not been created")
        self._container.reload()
        attributes = self._container.attrs
        labels = attributes.get("Config", {}).get("Labels", {})
        if (
            self._container.id != self._container_id
            or attributes.get("Id") != self._container_id
            or attributes.get("Name") != "/" + self._name
            or labels.get(OWNER_LABEL) != self._owner
            or labels.get("codehub.local/run") != self.run_id
        ):
            raise LinkControlError("Relay ownership changed; refusing mutation or removal")
        return self._container

    def _request(self, method: str, path: str, body: dict | None = None):
        container = self._owned_container()
        if container.attrs.get("State", {}).get("Status") != "running":
            raise LinkControlError("The owned relay is not running")
        if self._request_override is not None:
            return self._request_override(method, path, body)
        encoded = json.dumps(body, allow_nan=False).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://{self.control_bind}:{self.control_port}{path}",
            data=encoded,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        # Never send private control through HTTP_PROXY or an HTTP redirect.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=3) as response:
                content = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise LinkControlError("Link control request failed", status=exc.code) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise LinkControlError("Link control is unavailable") from exc
        if len(content) > MAX_RESPONSE_BYTES:
            raise LinkControlError("Link control response exceeded its bound")
        if not content:
            return None
        try:
            return json.loads(content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise LinkControlError("Link control returned invalid JSON") from exc

    def _proxy_name(self, spec: LinkSpec) -> str:
        if spec not in self._specs:
            raise ValueError("This link does not belong to the captured inventory")
        return f"link-{self._owner}-{spec.public_port}"

    def _proxy_config(self, spec: LinkSpec) -> dict:
        return {
            "name": self._proxy_name(spec),
            "listen": f"{self.data_bind}:{spec.public_port}",
            "upstream": f"{spec.upstream_host}:{spec.upstream_port}",
            "enabled": True,
        }

    def _check_proxy(self, spec: LinkSpec):
        expected = self._proxy_config(spec)
        actual = self._request("GET", "/proxies/" + expected["name"])
        if not isinstance(actual, dict) or any(actual.get(key) != value for key, value in expected.items()):
            raise LinkControlError("The owned link endpoint configuration changed")

    @_locked
    def start(self, specs: tuple[LinkSpec, ...] | None = None, *, baseline: LinkProfile = LinkProfile()) -> str:
        if self._state == "running":
            if specs is not None and specs != self._specs:
                raise LinkControlError("A running controller cannot adopt new links")
            self._owned_container()
            return self.inventory_json()
        if specs is not None:
            self.prepare(specs, baseline=baseline)
        if self._state != "prepared":
            raise LinkControlError("Prepare explicit link configuration before starting")
        if not rootless_workload_enabled() or trusted_docker_host() != self.trusted_host:
            raise LinkControlError("Links require the explicitly selected independent trusted Docker engine")
        boundary = validate_rootless_boundary()
        if boundary["runner_address"] != self.data_bind:
            raise LinkControlError("Data bind must equal the validated workload-reachable runner address")
        self._check_ports()
        self._client = self._client_factory(self.trusted_host)
        try:
            if self._client.info().get("ID") != boundary["trusted_engine"]:
                raise LinkControlError("Relay Docker client does not match the validated trusted engine")
            self._container = self._client.containers.create(
                TOXIPROXY_IMAGE,
                name=self._name,
                labels={OWNER_LABEL: self._owner, "codehub.local/run": self.run_id},
                command=["-host", self.control_bind, "-port", str(self.control_port)],
                network_mode="host",
                user="65532:65532",
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                read_only=True,
                privileged=False,
                nano_cpus=1_000_000_000,
                mem_limit=256 * 1024 * 1024,
                memswap_limit=256 * 1024 * 1024,
                pids_limit=64,
                restart_policy={"Name": "no"},
                log_config={"type": "json-file", "config": {"max-size": "1m", "max-file": "2"}},
                environment={"LOG_LEVEL": "warn"},
                detach=True,
            )
            self._container_id = self._container.id
            self._owned_container().start()
            deadline = time.monotonic() + self.readiness_seconds
            while True:
                try:
                    version = self._request("GET", "/version")
                    if not isinstance(version, dict) or version.get("version", "").removeprefix("v") != "2.12.0":
                        raise LinkControlError("Relay version does not match its pinned release")
                    break
                except LinkControlError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.1)
            if self._request("GET", "/proxies") != {}:
                raise LinkControlError("A new owned relay must have an empty proxy inventory")
            for spec in self._specs:
                self._request("POST", "/proxies", self._proxy_config(spec))
                self._check_proxy(spec)
            self._state = "running"
            for spec in self._specs:
                self.apply_profile(spec, self._baseline[spec])
            return self.inventory_json()
        except BaseException as exc:
            try:
                self.stop()
            except Exception as cleanup_error:
                exc.add_note(f"Owned relay cleanup also failed: {cleanup_error}")
            raise

    @_locked
    def apply_profile(self, spec: LinkSpec, profile: LinkProfile) -> None:
        if self._state != "running" or not isinstance(profile, LinkProfile):
            raise LinkControlError("An active owned link and validated profile are required")
        self._check_proxy(spec)
        path = "/proxies/" + self._proxy_name(spec) + "/toxics"
        for name in tuple(sorted(self._toxics[spec])):
            try:
                self._request("DELETE", path + "/" + name)
            except LinkControlError as exc:
                if exc.status != 404:
                    raise
            self._toxics[spec].discard(name)
        settings = []
        if profile.latency_ms:
            settings.append(("latency", {"latency": profile.latency_ms, "jitter": profile.jitter_ms}))
        if profile.bandwidth_kbps is not None:
            settings.append(("bandwidth", {"rate": profile.bandwidth_kbps}))
        if profile.reset_timeout_ms is not None:
            settings.append(("reset_peer", {"timeout": profile.reset_timeout_ms}))
        for stream in ("upstream", "downstream"):
            for kind, attributes in settings:
                name = f"owned-{self._owner}-{kind}-{stream}"
                # Record before mutation: a response timeout may follow success.
                self._toxics[spec].add(name)
                self._request(
                    "POST",
                    path,
                    {"name": name, "type": kind, "stream": stream, "toxicity": 1.0, "attributes": attributes},
                )

    @_locked
    def restore(self, spec: LinkSpec | None = None) -> None:
        """Restore the configured healthy baseline, without a global API reset."""
        selected = (spec,) if spec is not None else self._specs
        failures = []
        for link in selected:
            self._proxy_name(link)
            try:
                self.apply_profile(link, self._baseline[link])
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise ExceptionGroup("Some owned links could not restore their healthy profile", failures)

    @_locked
    def endpoint(self, spec: LinkSpec) -> tuple[str, int]:
        self._proxy_name(spec)
        return self.data_bind, spec.public_port

    @_locked
    def inventory_json(self) -> str:
        """Only ordinary data endpoints may be copied into workload configuration."""
        return json.dumps(
            [
                {
                    "source_region": spec.source_region,
                    "target_region": spec.target_region,
                    "group": spec.group,
                    "host": self.data_bind,
                    "port": spec.public_port,
                }
                for spec in self._specs
            ],
            sort_keys=True,
            allow_nan=False,
        )

    @_locked
    def stop(self) -> None:
        """Remove by immutable container ID after checking both ownership labels."""
        if self._container is not None:
            try:
                container = self._owned_container()
                container.remove(force=True)
            except Exception as exc:
                # A missing captured ID is idempotent; other failures stay visible.
                if not isinstance(exc, NotFound):
                    raise
            self._container = None
            self._container_id = None
        if self._client is not None:
            self._client.close()
            self._client = None
        self._state = "stopped"

    cleanup = stop
