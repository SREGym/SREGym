"""Own the private Docker network, outbound proxy, and its certificates."""

from __future__ import annotations

import json
import logging
import os
import shutil
import ssl
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from sregym.service.internet_policy import EndpointRule

logger = logging.getLogger("all.sregym.docker_egress")

DEFAULT_EGRESS_PROXY_IMAGE = "mitmproxy/mitmproxy:12.2.3"
EGRESS_PROXY_PORT = 8080
PROXY_CA_CONTAINER_PATH = "/etc/evaluation-egress/mitmproxy-ca-cert.pem"
PROXY_BUNDLE_CONTAINER_PATH = "/etc/evaluation-egress/ca-certificates.crt"


def _find_host_ca_bundle() -> Path:
    """Find a usable host CA bundle on common Linux, macOS, and WSL images."""
    candidates: list[str] = []
    configured = os.environ.get("SSL_CERT_FILE")
    if configured:
        candidates.append(configured)
    default_paths = ssl.get_default_verify_paths()
    if default_paths.cafile:
        candidates.append(default_paths.cafile)
    candidates.extend(
        [
            "/etc/ssl/certs/ca-certificates.crt",
            "/etc/pki/tls/certs/ca-bundle.crt",
            "/etc/ssl/cert.pem",
        ]
    )
    try:
        import certifi

        candidates.append(certifi.where())
    except ImportError:
        pass

    for candidate in dict.fromkeys(candidates):
        path = Path(candidate)
        if path.is_file() and path.stat().st_size > 0:
            return path
    raise FileNotFoundError("Could not find a readable host CA bundle")


class DockerEgress:
    def __init__(self, image: str = DEFAULT_EGRESS_PROXY_IMAGE):
        self.image = image
        self._network_name: str | None = None
        self._proxy_name: str | None = None
        self._tmp_dir: Path | None = None
        self._proxy_ca: Path | None = None
        self._ca_bundle: Path | None = None
        self._rules: tuple[EndpointRule, ...] = ()

    def _require_ready(self) -> None:
        if not all((self._network_name, self._proxy_name, self._proxy_ca, self._ca_bundle)):
            raise RuntimeError("Filtered egress proxy is not ready")

    def docker_args(self) -> list[str]:
        self._require_ready()
        return [
            f"--network={self._network_name}",
            "--add-host=host.docker.internal:host-gateway",
            "-v",
            f"{self._proxy_ca}:{PROXY_CA_CONTAINER_PATH}:ro",
            "-v",
            f"{self._ca_bundle}:{PROXY_BUNDLE_CONTAINER_PATH}:ro",
        ]

    def environment(self) -> dict[str, str]:
        self._require_ready()
        proxy_url = f"http://{self._proxy_name}:{EGRESS_PROXY_PORT}"
        return {
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "http_proxy": proxy_url,
            "https_proxy": proxy_url,
            # Internal Kubernetes HTTPS uses the proxy's CONNECT tunnel.
            "NO_PROXY": "",
            "no_proxy": "",
            "SSL_CERT_FILE": PROXY_BUNDLE_CONTAINER_PATH,
            "REQUESTS_CA_BUNDLE": PROXY_BUNDLE_CONTAINER_PATH,
            "CURL_CA_BUNDLE": PROXY_BUNDLE_CONTAINER_PATH,
            "GIT_SSL_CAINFO": PROXY_BUNDLE_CONTAINER_PATH,
            "NODE_EXTRA_CA_CERTS": PROXY_CA_CONTAINER_PATH,
        }

    def blocked_request_count(self) -> int:
        if self._tmp_dir is None:
            return 0
        log_path = self._tmp_dir / "blocked-requests.jsonl"
        try:
            with log_path.open(encoding="utf-8") as handle:
                return sum(1 for line in handle if line.strip())
        except FileNotFoundError:
            return 0

    def ensure_started(self, rules: tuple[EndpointRule, ...]) -> None:
        if self._proxy_name is not None:
            running = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", self._proxy_name],
                capture_output=True,
                text=True,
            )
            if running.returncode == 0 and running.stdout.strip() == "true":
                return
            raise RuntimeError("Filtered egress proxy is not running; refusing to start an unrestricted agent")

        self._ensure_proxy_image_exists()
        suffix = uuid.uuid4().hex[:8]
        self._network_name = f"evaluation-egress-{suffix}"
        self._proxy_name = f"evaluation-egress-proxy-{suffix}"
        self._rules = rules

        repo_root = Path(__file__).resolve().parents[2]
        addon_path = repo_root / "docker" / "egress_proxy.py"
        policy_path = repo_root / "sregym" / "service" / "internet_policy.py"
        if not addon_path.is_file() or not policy_path.is_file():
            self.close()
            raise FileNotFoundError("Egress proxy policy files are missing")

        try:
            self._prepare_state()
            self._run_docker_checked(
                ["docker", "network", "create", "--internal", self._network_name],
                "create the filtered egress network",
            )
            self._run_docker_checked(
                [
                    "docker",
                    "run",
                    "-d",
                    "--rm",
                    "--name",
                    self._proxy_name,
                    "--network",
                    self._network_name,
                    "--add-host=host.docker.internal:host-gateway",
                    "-v",
                    f"{addon_path}:/addons/egress_proxy.py:ro",
                    "-v",
                    f"{policy_path}:/addons/internet_policy.py:ro",
                    "-v",
                    f"{self._tmp_dir}:/state",
                    "-e",
                    "BLOCKED_REQUEST_LOG=/state/blocked-requests.jsonl",
                    "-e",
                    "EGRESS_POLICY_STATE=/state/policy.json",
                    "-e",
                    "PYTHONPATH=/addons",
                    self.image,
                    "mitmdump",
                    "--listen-host",
                    "0.0.0.0",
                    "--listen-port",
                    str(EGRESS_PROXY_PORT),
                    "--set",
                    "connection_strategy=lazy",
                    # Keep the local Kubernetes TLS connection as an
                    # end-to-end tunnel. The agent trusts the per-run
                    # certificate from its kubeconfig; mitmproxy must not
                    # replace it with an egress certificate.
                    "--set",
                    r"ignore_hosts=^host\.docker\.internal:16443$",
                    "-s",
                    "/addons/egress_proxy.py",
                ],
                "start the filtered egress proxy",
            )
            self._run_docker_checked(
                ["docker", "network", "connect", "bridge", self._proxy_name],
                "connect the egress proxy to the internet",
            )
            self._copy_proxy_certificate()
            logger.info(
                "Agent egress permits only: %s",
                ", ".join(f"{rule.host}:{rule.port}" for rule in self._rules),
            )
        except BaseException:
            self.close()
            raise

    def _prepare_state(self) -> None:
        """Create the fixed allowlist and blocked-request audit log."""
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="evaluation-egress-"))
        blocked_log = self._tmp_dir / "blocked-requests.jsonl"
        blocked_log.touch()
        self._write_policy(self._rules)

        # The mitmproxy image drops privileges before loading the addon. Allow
        # it to traverse the private temp directory and append to the audit log
        # without making the audit log readable by other host users.
        self._tmp_dir.chmod(0o711)
        blocked_log.chmod(0o622)

    def _write_policy(self, rules: tuple[EndpointRule, ...]) -> None:
        if self._tmp_dir is None:
            raise RuntimeError("Filtered egress state is not initialized")
        policy_path = self._tmp_dir / "policy.json"
        temp_path = self._tmp_dir / "policy.json.tmp"
        temp_path.write_text(
            json.dumps(
                {
                    "allowed_endpoints": [rule.to_dict() for rule in sorted(set(rules))],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temp_path.chmod(0o644)
        temp_path.replace(policy_path)

    def _ensure_proxy_image_exists(self) -> None:
        image = self.image
        inspected = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
        if inspected.returncode == 0:
            return
        logger.info("Pulling filtered egress proxy image '%s'...", image)
        self._run_docker_checked(["docker", "pull", image], "pull the filtered egress proxy image")

    def _copy_proxy_certificate(self) -> None:
        if self._proxy_name is None or self._tmp_dir is None:
            raise RuntimeError("Filtered egress proxy is not initialized")

        proxy_ca = self._tmp_dir / "mitmproxy-ca-cert.pem"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            copied = subprocess.run(
                [
                    "docker",
                    "cp",
                    f"{self._proxy_name}:/home/mitmproxy/.mitmproxy/mitmproxy-ca-cert.pem",
                    str(proxy_ca),
                ],
                capture_output=True,
            )
            if copied.returncode == 0 and proxy_ca.is_file() and proxy_ca.stat().st_size > 0:
                break
            running = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", self._proxy_name],
                capture_output=True,
                text=True,
            )
            if running.returncode != 0 or running.stdout.strip() != "true":
                logs = subprocess.run(
                    ["docker", "logs", self._proxy_name],
                    capture_output=True,
                    text=True,
                )
                raise RuntimeError(f"Filtered egress proxy stopped during startup: {logs.stderr or logs.stdout}")
            time.sleep(0.25)
        else:
            raise TimeoutError("Timed out while waiting for the filtered egress proxy certificate")

        system_bundle = _find_host_ca_bundle()
        combined_bundle = self._tmp_dir / "ca-certificates.crt"
        combined_bundle.write_bytes(system_bundle.read_bytes() + b"\n" + proxy_ca.read_bytes())
        self._proxy_ca = proxy_ca
        self._ca_bundle = combined_bundle

    @staticmethod
    def _run_docker_checked(command: list[str], action: str) -> subprocess.CompletedProcess:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"Could not {action}: {detail}")
        return result

    def close(self) -> None:
        if self._proxy_name:
            subprocess.run(
                ["docker", "rm", "-f", self._proxy_name],
                capture_output=True,
            )
        if self._network_name:
            subprocess.run(
                ["docker", "network", "rm", self._network_name],
                capture_output=True,
            )
        if self._tmp_dir:
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
        self._network_name = None
        self._proxy_name = None
        self._tmp_dir = None
        self._proxy_ca = None
        self._ca_bundle = None
        self._rules = ()
