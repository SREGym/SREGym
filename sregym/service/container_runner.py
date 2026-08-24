import contextlib
import logging
import os
import platform
import shutil
import ssl
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml

from sregym.service.internet_policy import InternetPolicy

logger = logging.getLogger("all.sregym.container_runner")

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
DEFAULT_EGRESS_PROXY_IMAGE = "mitmproxy/mitmproxy:12.2.3"
EGRESS_PROXY_PORT = 8080
PROXY_CA_CONTAINER_PATH = "/etc/evaluation-egress/mitmproxy-ca-cert.pem"
PROXY_BUNDLE_CONTAINER_PATH = "/etc/evaluation-egress/ca-certificates.crt"


def _docker_uses_separate_host() -> bool:
    """Return whether Docker runs outside the host's network namespace."""
    return platform.system() == "Darwin" or "microsoft" in platform.release().lower()


def _replace_loopback_host(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.hostname not in LOOPBACK_HOSTS:
        return url

    userinfo, separator, _ = parsed.netloc.rpartition("@")
    netloc = f"{userinfo}{separator}host.docker.internal"
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    return urlunsplit(parsed._replace(netloc=netloc))


def get_container_host_bind_address() -> str:
    """Return a host bind address reachable from the agent container."""
    if platform.system() != "Linux":
        # Docker Desktop exposes the host to containers through
        # host.docker.internal. The proxy must therefore bind beyond the host
        # loopback interface; its per-run bearer token prevents unauthenticated
        # access.
        return "0.0.0.0"
    result = subprocess.run(
        ["docker", "network", "inspect", "bridge", "-f", "{{(index .IPAM.Config 0).Gateway}}"],
        capture_output=True,
        text=True,
    )
    address = result.stdout.strip()
    if result.returncode != 0 or not address:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Could not determine the Docker host gateway: {detail}")
    return address


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


@dataclass
class ExecInput:
    command: str
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    timeout: int | None = None  # seconds, None = no timeout
    label: str = ""
    container_name: str = ""


@dataclass
class ContainerConfig:
    image: str = "sregym-agent-base:latest"
    network_mode: str = "host"
    kubeconfig_path: Path | None = None
    workspace_path: Path | None = None  # bind-mounted to /workspace for agent output
    logs_path: Path | None = None
    sregym_apps_path: Path | None = None
    sregym_app_subdirs: list[str] | None = None
    env_vars: dict = field(default_factory=dict)
    cpus: float = 4.0
    memory: str = "8g"
    internet_policy: InternetPolicy = field(default_factory=InternetPolicy)
    egress_proxy_image: str = DEFAULT_EGRESS_PROXY_IMAGE


class ContainerRunner:
    # Env vars forwarded from host to agent containers.
    # Sourced from litellm provider source code (llms/<provider>/).
    API_KEY_VARS = [
        # OpenAI
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "OPENAI_BASE_URL",
        # DeepSeek
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_API_BASE",
        # Anthropic
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_API_BASE",
        # Gemini / Google
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GEMINI_API_BASE",
        # Azure OpenAI
        "AZURE_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "AZURE_API_BASE",
        "AZURE_API_VERSION",
        "AZURE_AD_TOKEN",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
        "AZURE_TENANT_ID",
        "AZURE_USERNAME",
        "AZURE_PASSWORD",
        "AZURE_CERTIFICATE_PATH",
        "AZURE_CERTIFICATE_PASSWORD",
        "AZURE_CREDENTIAL",
        "AZURE_SCOPE",
        "AZURE_AUTHORITY_HOST",
        "AZURE_FEDERATED_TOKEN_FILE",
        # AWS Bedrock
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_REGION_NAME",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_SESSION_NAME",
        "AWS_PROFILE",
        "AWS_PROFILE_NAME",
        "AWS_ROLE_NAME",
        "AWS_ROLE_ARN",
        "AWS_WEB_IDENTITY_TOKEN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_STS_ENDPOINT",
        "AWS_EXTERNAL_ID",
        "AWS_BEDROCK_RUNTIME_ENDPOINT",
        "AWS_BEARER_TOKEN_BEDROCK",
        # WatsonX / IBM
        "WATSONX_API_KEY",
        "WATSONX_APIKEY",
        "WATSONX_API_BASE",
        "WATSONX_URL",
        "WATSONX_TOKEN",
        "WATSONX_PROJECT_ID",
        "WATSONX_REGION",
        "WATSONX_SPACE_ID",
        "WATSONX_DEPLOYMENT_SPACE_ID",
        "WATSONX_IAM_URL",
        "WATSONX_ZENAPIKEY",
        "WX_API_KEY",
        "WX_PROJECT_ID",
        "WX_URL",
        "WX_REGION",
        "WX_SPACE_ID",
        "WML_URL",
        # Vertex AI
        "VERTEXAI_PROJECT",
        "VERTEXAI_LOCATION",
        "VERTEX_LOCATION",
        "VERTEXAI_CREDENTIALS",
        "GOOGLE_APPLICATION_CREDENTIALS",
        # Moonshot
        "MOONSHOT_API_KEY",
        "MOONSHOT_API_BASE",
        # GLM
        "GLM_API_KEY",
        "ZAI_API_KEY",
        "ZHIPU_API_KEY",
        # Claude Code
        "CLAUDE_CODE_OAUTH_TOKEN",
        # GitHub Copilot CLI
        "COPILOT_GITHUB_TOKEN",
        "COPILOT_PROVIDER_BASE_URL",
        "COPILOT_PROVIDER_API_KEY",
        "COPILOT_PROVIDER_TYPE",
        # SREGym internal
        "AGENT_MODEL_ID",
        "AGENT_REASONING_EFFORT",
        "AGENT_API_BASE",
        "AGENT_API_KEY",
        "JUDGE_MODEL_ID",
        "JUDGE_API_BASE",
        "JUDGE_API_KEY",
        # Config vars
        "API_HOSTNAME",
        "API_PORT",
        "MCP_SERVER_PORT",
        "MCP_SERVER_URL",
        "EXPOSE_SERVER",
        "SESSION_CACHE_SIZE",
        "SESSION_TTL",
        "LLM_QUERY_MAX_RETRIES",
        "LLM_QUERY_INIT_RETRY_DELAY",
        "WAIT_FOR_POD_READY_TIMEOUT",
    ]

    def __init__(self, config: ContainerConfig | None = None):
        self.config = config or ContainerConfig()
        self._credential_tmps: list[str] = []
        self._egress_network_name: str | None = None
        self._egress_proxy_name: str | None = None
        self._egress_tmp_dir: Path | None = None
        self._egress_proxy_ca: Path | None = None
        self._egress_ca_bundle: Path | None = None

    @property
    def internet_access_mode(self) -> str:
        return self.config.internet_policy.mode.value

    def blocked_request_count(self) -> int:
        if self._egress_tmp_dir is None:
            return 0
        log_path = self._egress_tmp_dir / "blocked-requests.jsonl"
        try:
            with log_path.open(encoding="utf-8") as handle:
                return sum(1 for line in handle if line.strip())
        except FileNotFoundError:
            return 0

    def _ensure_filtered_egress(self) -> None:
        if not self.config.internet_policy.is_filtered:
            return
        if self._egress_proxy_name is not None:
            running = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", self._egress_proxy_name],
                capture_output=True,
                text=True,
            )
            if running.returncode == 0 and running.stdout.strip() == "true":
                return
            raise RuntimeError("Filtered egress proxy is not running; refusing to start an unrestricted agent")

        self._ensure_proxy_image_exists()
        suffix = uuid.uuid4().hex[:8]
        self._egress_network_name = f"evaluation-egress-{suffix}"
        self._egress_proxy_name = f"evaluation-egress-proxy-{suffix}"
        self._prepare_egress_state()

        repo_root = Path(__file__).resolve().parents[2]
        addon_path = repo_root / "docker" / "egress_proxy.py"
        policy_path = repo_root / "sregym" / "service" / "internet_policy.py"
        if not addon_path.is_file() or not policy_path.is_file():
            self.cleanup_egress_proxy()
            raise FileNotFoundError("Egress proxy policy files are missing")

        try:
            self._run_docker_checked(
                ["docker", "network", "create", "--internal", self._egress_network_name],
                "create the filtered egress network",
            )
            owners = ",".join(self.config.internet_policy.blocked_github_owners)
            repository_ids = ",".join(self.config.internet_policy.blocked_github_repository_ids)
            repositories = ",".join(self.config.internet_policy.blocked_github_repositories)
            self._run_docker_checked(
                [
                    "docker",
                    "run",
                    "-d",
                    "--rm",
                    "--name",
                    self._egress_proxy_name,
                    "--network",
                    self._egress_network_name,
                    "--add-host=host.docker.internal:host-gateway",
                    "-v",
                    f"{addon_path}:/addons/egress_proxy.py:ro",
                    "-v",
                    f"{policy_path}:/addons/internet_policy.py:ro",
                    "-v",
                    f"{self._egress_tmp_dir}:/state",
                    "-e",
                    f"BLOCKED_GITHUB_OWNERS={owners}",
                    "-e",
                    f"BLOCKED_GITHUB_REPOSITORY_IDS={repository_ids}",
                    "-e",
                    f"BLOCKED_GITHUB_REPOSITORIES={repositories}",
                    "-e",
                    "BLOCKED_REQUEST_LOG=/state/blocked-requests.jsonl",
                    "-e",
                    "PYTHONPATH=/addons",
                    self.config.egress_proxy_image,
                    "mitmdump",
                    "--listen-host",
                    "0.0.0.0",
                    "--listen-port",
                    str(EGRESS_PROXY_PORT),
                    "--set",
                    "connection_strategy=lazy",
                    "-s",
                    "/addons/egress_proxy.py",
                ],
                "start the filtered egress proxy",
            )
            self._run_docker_checked(
                ["docker", "network", "connect", "bridge", self._egress_proxy_name],
                "connect the egress proxy to the internet",
            )
            self._copy_proxy_certificate()
        except Exception:
            self.cleanup_egress_proxy()
            raise

    def _prepare_egress_state(self) -> None:
        """Create an audit log that the unprivileged proxy process can append to."""
        self._egress_tmp_dir = Path(tempfile.mkdtemp(prefix="evaluation-egress-"))
        blocked_log = self._egress_tmp_dir / "blocked-requests.jsonl"
        blocked_log.touch()

        # The mitmproxy image drops privileges before loading the addon. Allow
        # it to traverse the private temp directory and append to this one file
        # without making the audit log readable by other host users.
        self._egress_tmp_dir.chmod(0o711)
        blocked_log.chmod(0o622)

    def _ensure_proxy_image_exists(self) -> None:
        image = self.config.egress_proxy_image
        inspected = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
        if inspected.returncode == 0:
            return
        logger.info("Pulling filtered egress proxy image '%s'...", image)
        self._run_docker_checked(["docker", "pull", image], "pull the filtered egress proxy image")

    def _copy_proxy_certificate(self) -> None:
        if self._egress_proxy_name is None or self._egress_tmp_dir is None:
            raise RuntimeError("Filtered egress proxy is not initialized")

        proxy_ca = self._egress_tmp_dir / "mitmproxy-ca-cert.pem"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            copied = subprocess.run(
                [
                    "docker",
                    "cp",
                    f"{self._egress_proxy_name}:/home/mitmproxy/.mitmproxy/mitmproxy-ca-cert.pem",
                    str(proxy_ca),
                ],
                capture_output=True,
            )
            if copied.returncode == 0 and proxy_ca.is_file() and proxy_ca.stat().st_size > 0:
                break
            running = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", self._egress_proxy_name],
                capture_output=True,
                text=True,
            )
            if running.returncode != 0 or running.stdout.strip() != "true":
                logs = subprocess.run(
                    ["docker", "logs", self._egress_proxy_name],
                    capture_output=True,
                    text=True,
                )
                raise RuntimeError(f"Filtered egress proxy stopped during startup: {logs.stderr or logs.stdout}")
            time.sleep(0.25)
        else:
            raise TimeoutError("Timed out while waiting for the filtered egress proxy certificate")

        system_bundle = _find_host_ca_bundle()
        combined_bundle = self._egress_tmp_dir / "ca-certificates.crt"
        combined_bundle.write_bytes(system_bundle.read_bytes() + b"\n" + proxy_ca.read_bytes())
        self._egress_proxy_ca = proxy_ca
        self._egress_ca_bundle = combined_bundle

    @staticmethod
    def _run_docker_checked(command: list[str], action: str) -> subprocess.CompletedProcess:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"Could not {action}: {detail}")
        return result

    def cleanup_egress_proxy(self) -> None:
        if self._egress_proxy_name:
            subprocess.run(
                ["docker", "rm", "-f", self._egress_proxy_name],
                capture_output=True,
            )
        if self._egress_network_name:
            subprocess.run(
                ["docker", "network", "rm", self._egress_network_name],
                capture_output=True,
            )
        if self._egress_tmp_dir:
            shutil.rmtree(self._egress_tmp_dir, ignore_errors=True)
        self._egress_network_name = None
        self._egress_proxy_name = None
        self._egress_tmp_dir = None
        self._egress_proxy_ca = None
        self._egress_ca_bundle = None

    def _mount_codex_credentials(self, args: list[str]) -> None:
        """Mount Codex auth into a throwaway tempdir.

        Only the copied auth file is mounted from the host. Codex can write its
        generated state elsewhere in /root/.codex, but that state remains in
        the disposable container instead of making the host tempdir root-owned.
        """
        codex_dir = Path.home() / ".codex"
        if not codex_dir.is_dir():
            return

        tmp = tempfile.mkdtemp(prefix="sregym-codex-")
        auth_src = codex_dir / "auth.json"
        # Reject symlinks + check readability (files may be root-owned from container writes).
        if auth_src.is_symlink() or not auth_src.is_file() or not os.access(auth_src, os.R_OK):
            shutil.rmtree(tmp, ignore_errors=True)
            return

        auth_dst = Path(tmp) / "auth.json"
        shutil.copy2(auth_src, auth_dst)

        args.extend(["-v", f"{auth_dst}:/root/.codex/auth.json:ro"])
        self._credential_tmps.append(tmp)

    def cleanup_credential_tmps(self) -> None:
        """Remove throwaway credential directories."""
        for tmp in self._credential_tmps:
            shutil.rmtree(tmp, ignore_errors=True)
        self._credential_tmps = []

    def _build_env_flags(self, extra_env: dict[str, str] | None = None) -> list[str]:
        flags = []
        env_vars = dict(self.config.env_vars)

        # Forward API keys from host (skip empty values to avoid overriding
        # other auth mechanisms like OAuth subscription tokens)
        for var in self.API_KEY_VARS:
            if var in os.environ and var not in env_vars and os.environ[var]:
                env_vars[var] = os.environ[var]

        if extra_env:
            env_vars.update(extra_env)

        env_vars["AGENT_INTERNET_ACCESS"] = self.internet_access_mode
        if self.config.internet_policy.is_filtered:
            if self._egress_proxy_name is None or self._egress_proxy_ca is None or self._egress_ca_bundle is None:
                raise RuntimeError("Filtered egress proxy is not ready")
            proxy_url = f"http://{self._egress_proxy_name}:{EGRESS_PROXY_PORT}"
            env_vars.update(
                {
                    "HTTP_PROXY": proxy_url,
                    "HTTPS_PROXY": proxy_url,
                    "http_proxy": proxy_url,
                    "https_proxy": proxy_url,
                    "NO_PROXY": "",
                    "no_proxy": "",
                    "SSL_CERT_FILE": PROXY_BUNDLE_CONTAINER_PATH,
                    "REQUESTS_CA_BUNDLE": PROXY_BUNDLE_CONTAINER_PATH,
                    "CURL_CA_BUNDLE": PROXY_BUNDLE_CONTAINER_PATH,
                    "GIT_SSL_CAINFO": PROXY_BUNDLE_CONTAINER_PATH,
                    "NODE_EXTRA_CA_CERTS": PROXY_CA_CONTAINER_PATH,
                }
            )

        # Docker Desktop and filtered containers cannot reach host loopback
        # directly. Rewrite every forwarded provider endpoint that points to a
        # loopback address, not only the primary agent endpoint.
        if _docker_uses_separate_host() or self.config.internet_policy.is_filtered:
            for key, value in list(env_vars.items()):
                if not isinstance(value, str) or not (
                    key.endswith(("_API_BASE", "_BASE_URL", "_URL", "_ENDPOINT"))
                    or key in {"AGENT_API_BASE", "JUDGE_API_BASE"}
                ):
                    continue
                env_vars[key] = _replace_loopback_host(value)

        # Agent containers use Docker's host alias to reach SREGym services
        # running on the host, including the MCP port-forward.
        if self.config.network_mode == "host" or self.config.internet_policy.is_filtered:
            env_vars["API_HOSTNAME"] = "host.docker.internal"
            mcp_port = env_vars.get("MCP_SERVER_PORT", os.environ.get("MCP_SERVER_PORT", "9954"))
            env_vars["MCP_SERVER_URL"] = f"http://host.docker.internal:{mcp_port}"

        for key, value in env_vars.items():
            flags.extend(["-e", f"{key}={value}"])
        return flags

    def _build_base_docker_args(self) -> list[str]:
        args = [
            "docker",
            "run",
            "--rm",
            f"--cpus={self.config.cpus}",
            f"--memory={self.config.memory}",
        ]

        # Filtered agents have no direct external route. The proxy container is
        # the only member of their private network that also joins a public one.
        if self.config.internet_policy.is_filtered:
            if self._egress_network_name is None or self._egress_proxy_ca is None or self._egress_ca_bundle is None:
                raise RuntimeError("Filtered egress proxy is not ready")
            args.append(f"--network={self._egress_network_name}")
            args.append("--add-host=host.docker.internal:host-gateway")
            args.extend(["-v", f"{self._egress_proxy_ca}:{PROXY_CA_CONTAINER_PATH}:ro"])
            args.extend(["-v", f"{self._egress_ca_bundle}:{PROXY_BUNDLE_CONTAINER_PATH}:ro"])
        elif self.config.network_mode == "host":
            if platform.system() == "Darwin":
                # macOS: Don't use --network host (it's ignored), rely on host.docker.internal
                args.append("--add-host=host.docker.internal:host-gateway")
            else:
                # Linux: --network=host is unreliable in some configurations.
                # --add-host injects the host IP directly into /etc/hosts.
                args.append("--network=host")
                args.append("--add-host=host.docker.internal:host-gateway")
        else:
            args.append(f"--network={self.config.network_mode}")

        # Mount kubeconfig (read-only)
        if self.config.kubeconfig_path and self.config.kubeconfig_path.exists():
            kubeconfig_path = self._prepare_kubeconfig(self.config.kubeconfig_path)
            args.extend(["-v", f"{kubeconfig_path.resolve()}:/root/.kube/config:ro"])
            args.extend(["-e", "KUBECONFIG=/root/.kube/config"])

        # Mount AWS credentials directory (read-only) for Bedrock and other AWS services
        aws_dir = Path.home() / ".aws"
        if aws_dir.is_dir():
            args.extend(["-v", f"{aws_dir.resolve()}:/root/.aws:ro"])

        self._mount_codex_credentials(args)

        # Mount workspace directory for agent output (logs, results, trajectories)
        if self.config.workspace_path:
            self.config.workspace_path.mkdir(parents=True, exist_ok=True)
            args.extend(["-v", f"{self.config.workspace_path.resolve()}:/workspace"])

        # Mount logs directory (for composite command tee output)
        if self.config.logs_path:
            self.config.logs_path.mkdir(parents=True, exist_ok=True)
            args.extend(["-v", f"{self.config.logs_path.resolve()}:/logs"])

        # Mount only the needed SREGym-applications subdirectories (read-only)
        if self.config.sregym_apps_path and self.config.sregym_app_subdirs:
            for subdir in self.config.sregym_app_subdirs:
                host_path = self.config.sregym_apps_path / subdir
                if host_path.exists():
                    args.extend(["-v", f"{host_path.resolve()}:/opt/sregym/SREGym-applications/{subdir}:ro"])

        return args

    def _prepare_kubeconfig(self, source: Path) -> Path:
        if not self.config.internet_policy.is_filtered:
            return source

        with source.open(encoding="utf-8") as handle:
            config = yaml.safe_load(handle)
        changed = False
        for cluster in config.get("clusters", []):
            cluster_config = cluster.get("cluster", {})
            server = cluster_config.get("server")
            if isinstance(server, str):
                container_server = _replace_loopback_host(server)
                if container_server != server:
                    cluster_config["server"] = container_server
                    changed = True
        if not changed:
            return source

        temp_dir = Path(tempfile.mkdtemp(prefix="sregym-kubeconfig-"))
        output = temp_dir / source.name
        with output.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(config, handle, sort_keys=False)
        output.chmod(0o600)
        self._credential_tmps.append(str(temp_dir))
        return output

    def build_docker_command(self, exec_input: ExecInput) -> list[str]:
        self._ensure_filtered_egress()
        cmd = self._build_base_docker_args()
        suffix = uuid.uuid4().hex[:8]
        if exec_input.label:
            container_name = f"sregym-{exec_input.label}-{suffix}"
            cmd.extend(["--name", container_name])
            exec_input.container_name = container_name
        cmd.extend(self._build_env_flags(exec_input.env))
        cmd.append(self.config.image)
        cmd.append(exec_input.command)
        return cmd

    def build_composite_command(
        self,
        install_script: str | None,
        agent_version: str | None,
        driver_command: str,
    ) -> str:
        parts = []

        if install_script:
            version_env = f'AGENT_VERSION="{agent_version}" ' if agent_version else ""
            parts.append(
                f"{version_env}/opt/sregym/install-scripts/{install_script} 2>&1 "
                f"| tee /logs/install.log; INSTALL_RC=${{PIPESTATUS[0]}}; "
                f'echo "$INSTALL_RC" > /logs/install.rc; '
                f'[ "$INSTALL_RC" -eq 0 ] || exit "$INSTALL_RC"'
            )

        parts.append(
            f"{driver_command} 2>&1 "
            f"| tee /logs/driver.log; DRIVER_RC=${{PIPESTATUS[0]}}; "
            f'echo "$DRIVER_RC" > /logs/driver.rc; '
            f'exit "$DRIVER_RC"'
        )

        return " && ".join(parts)

    def run_sync(self, exec_input: ExecInput) -> subprocess.CompletedProcess:
        """Run a short-lived command in a container and wait for it to finish.

        Unlike run_async, this blocks until the container exits and returns the
        CompletedProcess with captured stdout/stderr.  Useful for pre-flight
        checks (e.g. model validation) that must complete before the main
        agent container is launched.
        """
        cmd = self.build_docker_command(exec_input)

        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=exec_input.timeout,
            )
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            if exec_input.container_name:
                ContainerRunner.stop_container(exec_input.container_name, timeout=5)
            raise
        finally:
            self.cleanup_credential_tmps()

    def run_async(self, exec_input: ExecInput) -> subprocess.Popen:
        """Start an agent in a container asynchronously. Returns Popen handle."""
        cmd = self.build_docker_command(exec_input)
        logger.info(f"Starting containerized agent [{exec_input.label}]: {exec_input.command[:80]}...")

        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

    def ensure_image_exists(self) -> None:
        """Check if the container image exists locally; build it if not."""
        image = self.config.image
        result = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
        )
        if result.returncode == 0:
            return

        logger.info(f"🐳 Container image '{image}' not found. Building automatically...")
        self.build_image()

    def build_image(self) -> None:
        """Build (or rebuild) the container image using docker/agents/build.sh."""
        image = self.config.image
        logger.info(f"🐳 Building container image '{image}'...")

        repo_root = Path(__file__).resolve().parent.parent.parent
        build_script = repo_root / "docker" / "agents" / "build.sh"

        if not build_script.exists():
            raise FileNotFoundError(
                f"Build script not found at {build_script}. Cannot auto-build container image '{image}'."
            )

        build_script.chmod(build_script.stat().st_mode | 0o755)
        result = subprocess.run(
            [str(build_script)],
            cwd=str(repo_root),
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to build container image '{image}'. Check the build output above for errors.")
        logger.info(f"✅ Container image '{image}' built successfully.")

    @staticmethod
    def stop_container(container_name: str, timeout: int = 10) -> None:
        """Stop a running container by name. Used for cleanup."""
        try:
            subprocess.run(
                ["docker", "stop", "-t", str(timeout), container_name],
                capture_output=True,
                timeout=timeout + 5,
            )
        except Exception:
            # Force remove if stop fails
            try:
                subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    capture_output=True,
                    timeout=5,
                )
            except Exception:
                # Force remove if stop fails
                with contextlib.suppress(Exception):
                    subprocess.run(
                        ["docker", "rm", "-f", container_name],
                        capture_output=True,
                        timeout=5,
                    )
