import contextlib
import json
import logging
import os
import platform
import shlex
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import yaml

from sregym.service.docker_egress import DEFAULT_EGRESS_PROXY_IMAGE, DockerEgress
from sregym.service.internet_policy import EndpointRule, InternetPolicy
from sregym.service.provider_endpoints import provider_endpoint_rules

logger = logging.getLogger("all.sregym.container_runner")

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
DEFAULT_AGENT_IMAGE = (
    "ghcr.io/sregym/agent-base:sha-b97d6810e994bb7354b4bc15c6bc81cb66e816b9"
    "@sha256:92e8b52af763c6e165144d314ec25b1ad3ba0e0f1b0a28ffa4a1760e92ec6b61"
)
LOCAL_AGENT_IMAGE = "sregym-agent-base:latest"
HARDENING_FLAGS = (
    "--cap-drop=ALL",
    "--cap-add=DAC_OVERRIDE",
    "--security-opt=no-new-privileges",
)
AGENT_TOOLS_CONTAINER_PATH = "/opt/agent-tools"
CONTAINER_PATH = f"{AGENT_TOOLS_CONTAINER_PATH}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


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


def _codex_subscription_auth_available(auth_path: Path) -> bool:
    """Inspect the mounted credential format without exposing its contents."""
    if auth_path.is_symlink() or not auth_path.is_file() or not os.access(auth_path, os.R_OK):
        return False
    try:
        auth = json.loads(auth_path.read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(auth, dict):
        return False

    # Codex honors an explicit mode first. Legacy files infer API-key auth
    # from OPENAI_API_KEY, even when tokens are also present.
    mode = auth.get("auth_mode")
    if mode is not None:
        return mode in ("chatgpt", "chatgptAuthTokens")
    return auth.get("OPENAI_API_KEY") is None and isinstance(auth.get("tokens"), dict)


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
    image: str = DEFAULT_AGENT_IMAGE
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
    harden_container: bool = True
    k8s_proxy_port: int = 16443
    published_ports: list[str] = field(default_factory=list)
    forward_host_credentials: bool = True
    codex_auth: Literal["copy", "shared", "none"] = "copy"


class ContainerRunner:
    # Env vars forwarded from host to agent containers.
    # These cover LiteLLM and the provider integrations used by agent CLIs.
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
        "ANTHROPIC_BASE_URL",
        # Gemini / Google
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GEMINI_API_BASE",
        "GOOGLE_GENERATIVE_AI_API_KEY",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_GENAI_USE_VERTEXAI",
        # Azure OpenAI
        "AZURE_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "AZURE_API_BASE",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_RESOURCE_NAME",
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
        # OpenRouter / Groq / Mistral / xAI / Hugging Face
        "OPENROUTER_API_KEY",
        "OPENROUTER_API_BASE",
        "GROQ_API_KEY",
        "GROQ_API_BASE",
        "MISTRAL_API_KEY",
        "MISTRAL_API_BASE",
        "XAI_API_KEY",
        "XAI_API_BASE",
        "HF_TOKEN",
        "HF_ENDPOINT",
        "LLAMA_API_KEY",
        # GLM
        "GLM_API_KEY",
        "ZAI_API_KEY",
        "ZAI_API_BASE",
        "ZHIPU_API_KEY",
        # Cursor CLI
        "CURSOR_API_KEY",
        # Claude Code
        "CLAUDE_CODE_OAUTH_TOKEN",
        # GitHub Copilot CLI
        "COPILOT_GITHUB_TOKEN",
        "GITHUB_TOKEN",
        "COPILOT_PROVIDER_BASE_URL",
        "COPILOT_PROVIDER_API_KEY",
        "COPILOT_PROVIDER_TYPE",
        # SREGym internal
        "AGENT_MODEL_ID",
        "AGENT_REASONING_EFFORT",
        "AGENT_API_BASE",
        "AGENT_API_KEY",
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

    # Vars that select AWS credentials. Region vars are excluded on purpose:
    # they say where to call, not which identity to call with.
    AWS_CREDENTIAL_VARS = (
        "AWS_PROFILE",
        "AWS_PROFILE_NAME",
        "AWS_ACCESS_KEY_ID",
        "AWS_ROLE_ARN",
        "AWS_ROLE_NAME",
        "AWS_WEB_IDENTITY_TOKEN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_BEARER_TOKEN_BEDROCK",
    )

    # Only the agent model. The judge runs host-side in the conductor, where it
    # reads ~/.aws directly, so a Bedrock judge is no reason to mount anything
    # into an agent container that may only speak to OpenAI.
    MODEL_ID_VARS = ("AGENT_MODEL_ID",)

    # Model-id providers that resolve AWS credentials. "amazon-bedrock" is the
    # spelling OpenCode uses; see PROVIDER_ENV_VARS in clients/opencode.
    AWS_MODEL_PROVIDERS = ("bedrock", "amazon-bedrock", "sagemaker")

    def __init__(self, config: ContainerConfig | None = None):
        self.config = config or ContainerConfig()
        self._credential_tmps: list[str] = []
        self._egress = DockerEgress(self.config.egress_proxy_image, k8s_proxy_port=self.config.k8s_proxy_port)
        self._agent_tools_volume: str | None = None

    @property
    def internet_access_mode(self) -> str:
        return self.config.internet_policy.mode.value

    def blocked_request_count(self) -> int:
        return self._egress.blocked_request_count()

    def blocked_request_records(self, start: int = 0) -> list[dict[str, str | int]]:
        return self._egress.blocked_request_records(start)

    def _ensure_filtered_egress(self, env_vars: dict[str, str]) -> None:
        if self.config.internet_policy.is_filtered:
            self._egress.ensure_started(self._configured_egress_rules(env_vars))

    def cleanup_egress_proxy(self) -> None:
        self._egress.close()

    def close(self) -> None:
        """Release all per-run resources, including after partial startup."""
        try:
            self.cleanup_egress_proxy()
        finally:
            try:
                self.cleanup_credential_tmps()
            finally:
                self.cleanup_agent_tools()

    def _configured_egress_rules(self, env_vars: dict[str, str]) -> tuple[EndpointRule, ...]:
        rules = {
            EndpointRule("host.docker.internal", int(env_vars.get("API_PORT", "8000")), inspect_tools=False),
            EndpointRule("host.docker.internal", self.config.k8s_proxy_port, inspect_tools=False),
            EndpointRule("host.docker.internal", int(env_vars.get("MCP_SERVER_PORT", "9954")), inspect_tools=False),
        }
        rules.update(
            provider_endpoint_rules(
                self.config.internet_policy,
                env_vars,
                codex_subscription_auth=(
                    self.config.internet_policy.is_filtered
                    and (self.config.internet_policy.agent_name or "").casefold() == "codex"
                    and _codex_subscription_auth_available(Path.home() / ".codex" / "auth.json")
                ),
            )
        )
        rules.update(
            EndpointRule.from_url(_replace_loopback_host(endpoint))
            for endpoint in self.config.internet_policy.additional_allowed_endpoints
        )
        return tuple(sorted(rules))

    @property
    def has_prepared_agent_tools(self) -> bool:
        return self._agent_tools_volume is not None

    def prepare_agent_tools(self, install_script: str | None, agent_version: str | None) -> None:
        """Install an agent CLI before the evaluation network is available."""
        if not self.config.internet_policy.is_filtered or not install_script:
            return
        if self._agent_tools_volume is not None:
            return
        if Path(install_script).name != install_script:
            raise ValueError(f"Invalid agent install script: {install_script}")

        suffix = uuid.uuid4().hex[:8]
        volume = f"evaluation-agent-tools-{suffix}"
        self._run_docker_checked(["docker", "volume", "create", volume], "create the agent tools volume")
        self._agent_tools_volume = volume
        version = agent_version or "latest"
        command = f"AGENT_VERSION={shlex.quote(version)} /opt/sregym/install-scripts/{shlex.quote(install_script)}"
        try:
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network=bridge",
                    "-v",
                    f"{volume}:{AGENT_TOOLS_CONTAINER_PATH}",
                    "-e",
                    f"NPM_CONFIG_PREFIX={AGENT_TOOLS_CONTAINER_PATH}",
                    "-e",
                    f"PATH={CONTAINER_PATH}",
                    self.config.image,
                    command,
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()
                raise RuntimeError(f"Could not install the agent CLI: {detail}")
            logger.info("Prepared agent CLI before enabling evaluation network restrictions")
        except BaseException:
            self.cleanup_agent_tools()
            raise

    def cleanup_agent_tools(self) -> None:
        if self._agent_tools_volume:
            subprocess.run(
                ["docker", "volume", "rm", "-f", self._agent_tools_volume],
                capture_output=True,
            )
        self._agent_tools_volume = None

    @staticmethod
    def _run_docker_checked(command: list[str], action: str) -> subprocess.CompletedProcess:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"Could not {action}: {detail}")
        return result

    def _mount_codex_credentials(self, args: list[str]) -> None:
        """Copy auth read-only for agents; share only the auth file for judge refreshes."""
        if self.config.codex_auth == "none":
            return
        auth_src = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
        # Reject symlinks + check readability (files may be root-owned from container writes).
        if auth_src.is_symlink() or not auth_src.is_file() or not os.access(auth_src, os.R_OK):
            return

        if self.config.codex_auth == "shared":
            args.extend(["-v", f"{auth_src.resolve()}:/root/.codex/auth.json:rw"])
            return

        tmp = tempfile.mkdtemp(prefix="sregym-codex-")
        auth_dst = Path(tmp) / "auth.json"
        shutil.copy2(auth_src, auth_dst)

        args.extend(["-v", f"{auth_dst}:/root/.codex/auth.json:ro"])
        self._credential_tmps.append(tmp)

    def _run_uses_aws(self, extra_env: dict[str, str] | None = None) -> bool:
        """Whether this run resolves AWS credentials, and so needs ~/.aws."""

        def lookup(name: str) -> str:
            # First source that *defines* the var wins, matching how
            # _build_env_flags layers them. A caller setting it empty is
            # masking it, not deferring to the host.
            for source in (extra_env or {}, self.config.env_vars, os.environ):
                if name in source:
                    return str(source[name] or "")
            return ""

        def is_aws_model(model: str) -> bool:
            provider = model.split("/", 1)[0].casefold()
            return any(provider == p or provider.startswith(f"{p}-") for p in self.AWS_MODEL_PROVIDERS)

        if any(lookup(var) for var in self.AWS_CREDENTIAL_VARS):
            return True
        # A default profile in ~/.aws/config needs no AWS_* var set, so the
        # model id is the only signal left that an AWS run needs the mount.
        return any(is_aws_model(lookup(var)) for var in self.MODEL_ID_VARS)

    def cleanup_credential_tmps(self) -> None:
        """Remove throwaway credential directories."""
        for tmp in self._credential_tmps:
            shutil.rmtree(tmp, ignore_errors=True)
        self._credential_tmps = []

    def _build_env_vars(self, extra_env: dict[str, str] | None = None) -> dict[str, str]:
        env_vars = dict(self.config.env_vars)

        # Forward API keys from host (skip empty values to avoid overriding
        # other auth mechanisms like OAuth subscription tokens)
        for var in self.API_KEY_VARS if self.config.forward_host_credentials else ():
            if var in os.environ and var not in env_vars and os.environ[var]:
                env_vars[var] = os.environ[var]

        if extra_env:
            env_vars.update(extra_env)

        if self.config.internet_policy.agent_name == "codex" and env_vars.get("AGENT_API_BASE"):
            env_vars.setdefault("CODEX_HOME", "/logs")

        # The judge runs on the host. Its model and credentials are not inputs
        # to the evaluated agent and must not enter the agent container.
        for name in ("JUDGE_MODEL_ID", "JUDGE_API_BASE", "JUDGE_API_KEY"):
            env_vars.pop(name, None)

        if self.config.forward_host_credentials and os.environ.get("AGENT_JEV_MODEL"):
            env_vars.setdefault("AGENT_JEV_MODEL", os.environ["AGENT_JEV_MODEL"])
            env_vars.setdefault("TYPESAFE_API_KEY", os.environ.get("TYPESAFE_API_KEY", ""))

        env_vars["AGENT_INTERNET_ACCESS"] = self.internet_access_mode

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

        if self._agent_tools_volume is not None:
            env_vars["PATH"] = CONTAINER_PATH
            env_vars["NODE_PATH"] = f"{AGENT_TOOLS_CONTAINER_PATH}/lib/node_modules"

        return env_vars

    def _build_env_flags(self, extra_env: dict[str, str] | None = None) -> list[str]:
        env_vars = self._build_env_vars(extra_env)
        if self.config.internet_policy.is_filtered:
            env_vars.update(self._egress.environment())

        flags = []
        for key, value in env_vars.items():
            flags.extend(["-e", f"{key}={value}"])
        return flags

    def _build_base_docker_args(self, extra_env: dict[str, str] | None = None) -> list[str]:
        args = [
            "docker",
            "run",
            "--rm",
            f"--cpus={self.config.cpus}",
            f"--memory={self.config.memory}",
        ]

        if self.config.harden_container:
            args.extend(HARDENING_FLAGS)

        # Filtered agents have no direct external route. The proxy container is
        # the only member of their private network that also joins a public one.
        if self.config.internet_policy.is_filtered:
            args.extend(self._egress.docker_args())
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

        if self._agent_tools_volume:
            args.extend(["-v", f"{self._agent_tools_volume}:{AGENT_TOOLS_CONTAINER_PATH}:ro"])

        # Mount kubeconfig (read-only)
        if self.config.kubeconfig_path and self.config.kubeconfig_path.exists():
            kubeconfig_path = self._prepare_kubeconfig(self.config.kubeconfig_path)
            args.extend(["-v", f"{kubeconfig_path.resolve()}:/root/.kube/config:ro"])
            args.extend(["-e", "KUBECONFIG=/root/.kube/config"])

        # Gated on the run resolving AWS credentials: the directory holds live
        # SSO and CLI cache tokens, which a run against another provider has no
        # use for.
        aws_dir = Path.home() / ".aws"
        if self.config.forward_host_credentials and aws_dir.is_dir():
            if self._run_uses_aws(extra_env):
                args.extend(["-v", f"{aws_dir.resolve()}:/root/.aws:ro"])
            else:
                logger.debug("Skipping ~/.aws mount: no AWS credentials selected for this run")

        self._mount_codex_credentials(args)

        for port in self.config.published_ports:
            args.extend(["-p", port])

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
        env_vars = self._build_env_vars(exec_input.env)
        self._ensure_filtered_egress(env_vars)
        cmd = self._build_base_docker_args(exec_input.env)
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
        *,
        capture_logs: bool = True,
    ) -> str:
        parts = []

        if install_script and not self.has_prepared_agent_tools:
            version_env = f'AGENT_VERSION="{agent_version}" ' if agent_version else ""
            if not capture_logs:
                return f"{version_env}/opt/sregym/install-scripts/{install_script} > /dev/null 2>&1 && {driver_command}"
            parts.append(
                f"{version_env}/opt/sregym/install-scripts/{install_script} 2>&1 "
                f"| tee /logs/install.log; INSTALL_RC=${{PIPESTATUS[0]}}; "
                f'echo "$INSTALL_RC" > /logs/install.rc; '
                f'[ "$INSTALL_RC" -eq 0 ] || exit "$INSTALL_RC"'
            )

        if not capture_logs:
            return driver_command

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
        """Pull the selected release, or build the explicit local-development tag."""
        image = self.config.image
        result = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
        )
        if result.returncode == 0:
            return

        if image == LOCAL_AGENT_IMAGE:
            self.build_image()
            return
        logger.info("Pulling agent image '%s'...", image)
        self._run_docker_checked(["docker", "pull", image], f"pull agent image '{image}'")

    def build_image(self) -> None:
        """Build (or rebuild) the container image using docker/agents/build.sh."""
        # Rebuilding a release creates a local development image; never pretend
        # locally compiled bytes have the published release's digest.
        image = LOCAL_AGENT_IMAGE if self.config.image == DEFAULT_AGENT_IMAGE else self.config.image
        if "@" in image:
            raise ValueError("Cannot rebuild a digest-pinned custom image; select a local image tag first")
        logger.info(f"🐳 Building container image '{image}'...")

        repo_root = Path(__file__).resolve().parent.parent.parent
        build_script = repo_root / "docker" / "agents" / "build.sh"

        if not build_script.exists():
            raise FileNotFoundError(
                f"Build script not found at {build_script}. Cannot auto-build container image '{image}'."
            )

        result = subprocess.run(
            ["bash", str(build_script)],
            cwd=str(repo_root),
            env={**os.environ, "SREGYM_AGENT_IMAGE": image},
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to build container image '{image}'. Check the build output above for errors.")
        self.config.image = image
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
