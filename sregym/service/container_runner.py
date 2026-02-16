import contextlib
import logging
import os
import platform
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("all.sregym.container_runner")


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


class ContainerRunner:
    # API keys and config vars to forward from host environment
    API_KEY_VARS = [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "MODEL_ID",
        "AWS_PROFILE",
        "AWS_DEFAULT_REGION",
        "WATSONX_API_BASE",
        "WX_PROJECT_ID",
        "WATSONX_API_KEY",
        "AZURE_API_KEY",
        "AZURE_API_BASE",
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

    def _build_env_flags(self, extra_env: dict[str, str] | None = None) -> list[str]:
        flags = []
        env_vars = dict(self.config.env_vars)

        # Forward API keys from host
        for var in self.API_KEY_VARS:
            if var in os.environ and var not in env_vars:
                env_vars[var] = os.environ[var]

        if extra_env:
            env_vars.update(extra_env)

        # On macOS, --network host is a no-op so the container can't reach
        # the host via localhost.  Point API_HOSTNAME at the Docker Desktop
        # magic DNS name instead.
        if self.config.network_mode == "host" and platform.system() == "Darwin":
            env_vars.setdefault("API_HOSTNAME", "host.docker.internal")

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

        # --network host is silently ignored on macOS Docker Desktop.
        # Use bridge networking with host.docker.internal instead.
        if self.config.network_mode == "host" and platform.system() == "Darwin":
            args.append("--add-host=host.docker.internal:host-gateway")
        else:
            args.append(f"--network={self.config.network_mode}")

        # Mount kubeconfig (read-only)
        if self.config.kubeconfig_path and self.config.kubeconfig_path.exists():
            args.extend(["-v", f"{self.config.kubeconfig_path.resolve()}:/root/.kube/config:ro"])
            args.extend(["-e", "KUBECONFIG=/root/.kube/config"])

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

    def build_docker_command(self, exec_input: ExecInput) -> list[str]:
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
