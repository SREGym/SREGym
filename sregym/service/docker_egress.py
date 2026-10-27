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
PROXY_BLOCKED_LOG_PATH = "/state/blocked-requests.jsonl"
PROXY_CA_CONTAINER_PATH = "/etc/evaluation-egress/mitmproxy-ca-cert.pem"
PROXY_BUNDLE_CONTAINER_PATH = "/etc/evaluation-egress/ca-certificates.crt"
PROXY_CA_SOURCE_PATHS = (
    "/home/mitmproxy/.mitmproxy/mitmproxy-ca-cert.pem",
    "/root/.mitmproxy/mitmproxy-ca-cert.pem",
)
PROXY_BOOTSTRAP_SENTINEL = "/state/.sregym-proxy-ready"


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
    def __init__(self, image: str = DEFAULT_EGRESS_PROXY_IMAGE, *, k8s_proxy_port: int = 16443):
        self.image = image
        self.k8s_proxy_port = k8s_proxy_port
        self._network_name: str | None = None
        self._proxy_name: str | None = None
        self._cert_volume: str | None = None
        self._tmp_dir: Path | None = None
        self._proxy_ca: Path | None = None
        self._ca_bundle: Path | None = None
        self._rules: tuple[EndpointRule, ...] = ()

    def _require_ready(self) -> None:
        if not all(
            (
                self._network_name,
                self._proxy_name,
                self._cert_volume,
                self._proxy_ca,
                self._ca_bundle,
            )
        ):
            raise RuntimeError("Filtered egress proxy is not ready")

    def docker_args(self) -> list[str]:
        self._require_ready()
        return [
            f"--network={self._network_name}",
            "--add-host=host.docker.internal:host-gateway",
            "-v",
            f"{self._cert_volume}:/etc/evaluation-egress:ro",
        ]

    def environment(self) -> dict[str, str]:
        self._require_ready()
        proxy_url = f"http://{self._proxy_name}:{EGRESS_PROXY_PORT}"
        return {
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "http_proxy": proxy_url,
            "https_proxy": proxy_url,
            # A forwarded port or local tool lives in the agent container, not
            # in the separate proxy container. Host services still use CONNECT.
            "NO_PROXY": "localhost,127.0.0.1,::1",
            "no_proxy": "localhost,127.0.0.1,::1",
            "SSL_CERT_FILE": PROXY_BUNDLE_CONTAINER_PATH,
            "REQUESTS_CA_BUNDLE": PROXY_BUNDLE_CONTAINER_PATH,
            "CURL_CA_BUNDLE": PROXY_BUNDLE_CONTAINER_PATH,
            "GIT_SSL_CAINFO": PROXY_BUNDLE_CONTAINER_PATH,
            "NODE_EXTRA_CA_CERTS": PROXY_CA_CONTAINER_PATH,
        }

    def blocked_request_count(self) -> int:
        return sum(1 for line in self._blocked_request_lines(missing_ok=True) if line.strip())

    def blocked_request_records(self, start: int = 0) -> list[dict[str, str | int]]:
        """Read this attempt's records before the proxy state is removed."""
        records = []
        count = 0
        for line in self._blocked_request_lines():
            if not line.strip():
                continue
            count += 1
            if count <= start:
                continue
            record = json.loads(line)
            # Whitelist exported fields, including when reading an older log.
            records.append({key: record[key] for key in ("timestamp", "method", "scheme", "host", "port", "reason")})
        if count < start:
            raise ValueError("blocked-request log was truncated during the attempt")
        return records

    def _blocked_request_lines(self, *, missing_ok: bool = False) -> list[str]:
        """Read authoritative proxy state, including from a stopped container."""
        if self._proxy_name:
            try:
                result = subprocess.run(
                    ["docker", "exec", self._proxy_name, "cat", PROXY_BLOCKED_LOG_PATH],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0:
                    return result.stdout.splitlines()
                # docker exec requires a running container; docker cp does not.
                # Never substitute the untouched host bootstrap log for this audit.
                with tempfile.TemporaryDirectory(prefix="evaluation-egress-audit-") as directory:
                    log_path = Path(directory) / "blocked-requests.jsonl"
                    copied = subprocess.run(
                        ["docker", "cp", f"{self._proxy_name}:{PROXY_BLOCKED_LOG_PATH}", str(log_path)],
                        capture_output=True,
                        timeout=10,
                    )
                    if copied.returncode != 0:
                        raise OSError("Could not copy the filtered egress audit")
                    return log_path.read_text(encoding="utf-8").splitlines()
            except (OSError, subprocess.SubprocessError) as exc:
                raise OSError("Could not read the filtered egress proxy audit") from exc
        if self._tmp_dir is None:
            return []
        try:
            return (self._tmp_dir / "blocked-requests.jsonl").read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            if missing_ok:
                return []
            raise

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
        self._cert_volume = f"evaluation-egress-certs-{suffix}"
        self._rules = rules

        repo_root = Path(__file__).resolve().parents[2]
        addon_path = repo_root / "docker" / "egress_proxy.py"
        policy_path = repo_root / "sregym" / "service" / "internet_policy.py"
        if not addon_path.is_file() or not policy_path.is_file():
            self.close()
            raise FileNotFoundError("Egress proxy policy files are missing")

        try:
            self._prepare_state()
            state_dir = self._tmp_dir
            if state_dir is None:
                raise RuntimeError("Filtered egress state is not initialized")
            self._run_docker_checked(
                ["docker", "network", "create", "--internal", self._network_name],
                "create the filtered egress network",
            )
            self._run_docker_checked(
                ["docker", "volume", "create", self._cert_volume],
                "create the filtered egress certificate volume",
            )
            self._run_docker_checked(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    self._proxy_name,
                    "--network",
                    self._network_name,
                    "--add-host=host.docker.internal:host-gateway",
                    "-v",
                    f"{self._cert_volume}:/certs",
                    "-e",
                    f"BLOCKED_REQUEST_LOG={PROXY_BLOCKED_LOG_PATH}",
                    "-e",
                    "EGRESS_POLICY_STATE=/state/policy.json",
                    "-e",
                    "PYTHONPATH=/addons",
                    self.image,
                    "sh",
                    "-ec",
                    self._proxy_start_command(),
                ],
                "start the filtered egress proxy",
            )
            # The detached bootstrap shell may not have run yet when docker run
            # returns. Create the copy destinations synchronously before upload.
            self._run_docker_checked(
                ["docker", "exec", self._proxy_name, "mkdir", "-p", "/addons", "/state"],
                "prepare the filtered egress proxy directories",
            )
            self._copy_file_to_proxy(addon_path, "/addons/egress_proxy.py")
            self._copy_file_to_proxy(policy_path, "/addons/internet_policy.py")
            self._copy_file_to_proxy(state_dir / "policy.json", "/state/policy.json")
            self._run_docker_checked(
                ["docker", "exec", self._proxy_name, "touch", PROXY_BLOCKED_LOG_PATH, PROXY_BOOTSTRAP_SENTINEL],
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

    def _proxy_start_command(self) -> str:
        """Wait for copied policy files before starting the proxy process.

        Docker Desktop's Windows daemon cannot consume a WSL path as a bind
        mount, although the Docker client can upload the same path with
        ``docker cp``.  Starting a small bootstrap shell lets every supported
        Docker client copy the addon and policy into the container before
        mitmdump becomes its long-running process.
        """
        return " ".join(
            (
                f"while [ ! -f {PROXY_BOOTSTRAP_SENTINEL} ]; do sleep 0.05; done;",
                "exec mitmdump --listen-host 0.0.0.0",
                f"--listen-port {EGRESS_PROXY_PORT}",
                "--set connection_strategy=lazy",
                # Keep the local Kubernetes TLS connection as an end-to-end
                # tunnel. The agent trusts the per-run certificate from its
                # kubeconfig; mitmproxy must not replace it with an egress CA.
                f"--set 'ignore_hosts=^host\\.docker\\.internal:{self.k8s_proxy_port}$'",
                "-s /addons/egress_proxy.py",
            )
        )

    def _copy_file_to_proxy(self, source: Path, destination: str) -> None:
        if self._proxy_name is None:
            raise RuntimeError("Filtered egress proxy is not initialized")
        self._run_docker_checked(
            ["docker", "cp", str(source), f"{self._proxy_name}:{destination}"],
            f"copy {source.name} into the filtered egress proxy",
        )

    def _prepare_state(self) -> None:
        """Create the fixed allowlist and blocked-request audit log."""
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="evaluation-egress-"))
        blocked_log = self._tmp_dir / "blocked-requests.jsonl"
        blocked_log.touch()
        self._write_policy(self._rules)

        # Keep the host files private. They are copied into the proxy during
        # bootstrap instead of bind-mounted so this also works through Docker
        # Desktop from a WSL client.
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
            copied = None
            for container_path in PROXY_CA_SOURCE_PATHS:
                copied = subprocess.run(
                    ["docker", "cp", f"{self._proxy_name}:{container_path}", str(proxy_ca)],
                    capture_output=True,
                )
                if copied.returncode == 0 and proxy_ca.is_file() and proxy_ca.stat().st_size > 0:
                    break
            if copied and copied.returncode == 0 and proxy_ca.is_file() and proxy_ca.stat().st_size > 0:
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
        self._copy_file_to_proxy(combined_bundle, "/certs/ca-certificates.crt")
        self._copy_file_to_proxy(proxy_ca, "/certs/mitmproxy-ca-cert.pem")
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
            if self._tmp_dir:
                subprocess.run(
                    [
                        "docker",
                        "cp",
                        f"{self._proxy_name}:{PROXY_BLOCKED_LOG_PATH}",
                        str(self._tmp_dir / "blocked-requests.jsonl"),
                    ],
                    capture_output=True,
                )
            subprocess.run(
                ["docker", "rm", "-f", "-v", self._proxy_name],
                capture_output=True,
            )
        if self._network_name:
            subprocess.run(
                ["docker", "network", "rm", self._network_name],
                capture_output=True,
            )
        if self._cert_volume:
            subprocess.run(
                ["docker", "volume", "rm", self._cert_volume],
                capture_output=True,
            )
        if self._tmp_dir:
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
        self._network_name = None
        self._proxy_name = None
        self._cert_volume = None
        self._tmp_dir = None
        self._proxy_ca = None
        self._ca_bundle = None
        self._rules = ()
