"""Own the optional CLI judge for one benchmark invocation."""

import contextlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

from sregym.agent_registry import get_agent
from sregym.service.container_runner import ContainerConfig, ContainerRunner, ExecInput
from sregym.service.internet_policy import InternetPolicy

logger = logging.getLogger(__name__)
JUDGE_BACKENDS = ("api", "codex", "claudecode", "copilot", "cursor")
BRIDGE_PORT = 4100
STARTUP_TIMEOUT_SECONDS = 180
AUTH_VARIABLES = {
    "claudecode": "CLAUDE_CODE_OAUTH_TOKEN",
    "copilot": "COPILOT_GITHUB_TOKEN",
    "cursor": "CURSOR_API_KEY",
}


def _subscription_environment(backend: str) -> dict[str, str]:
    if backend == "codex":
        auth = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
        try:
            data = json.loads(auth.read_text()) if not auth.is_symlink() else {}
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict) or not data.get("tokens"):
            raise ValueError(f"Codex subscription credentials are required at {auth}, as for Codex agent runs")
        return {}
    variable = AUTH_VARIABLES[backend]
    if not os.environ.get(variable):
        raise ValueError(f"{variable} is required for the {backend} judge, as for {backend} agent runs")
    return {variable: os.environ[variable]}


def _wait_for_bridge(proc: subprocess.Popen, container: str, log_path: Path) -> str:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    opener = build_opener(ProxyHandler({}))
    url = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Judge container exited {proc.returncode}; see {log_path}")
        if url is None:
            port = subprocess.run(
                ["docker", "port", container, f"{BRIDGE_PORT}/tcp"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if port.returncode == 0 and port.stdout.strip():
                url = f"http://{port.stdout.strip()}"
        if url:
            try:
                with opener.open(f"{url}/health", timeout=1) as response:
                    if response.status == 200:
                        return f"{url}/v1"
            except OSError:
                pass
        time.sleep(0.25)
    raise TimeoutError(f"Judge bridge did not start within {STARTUP_TIMEOUT_SECONDS}s; see {log_path}")


@contextlib.contextmanager
def managed_judge_backend(backend: str = "api", *, force_build: bool = False):
    """Keep existing endpoint behavior for API runs; manage a CLI bridge when selected."""
    if backend == "api":
        yield
        return
    if backend not in JUDGE_BACKENDS:
        raise ValueError(f"Unknown judge backend: {backend}")

    env = _subscription_environment(backend)
    repo_root = Path(__file__).resolve().parents[2]
    registration = get_agent(backend, repo_root / "agents.yaml")
    if registration is None or not registration.install_script:
        raise ValueError(f"No CLI installer registered for judge backend {backend}")

    logs_root = Path("logs")
    logs_root.mkdir(exist_ok=True)
    logs = Path(tempfile.mkdtemp(prefix=f"judge-{backend}-", dir=logs_root)).resolve()
    # Released images can predate this bridge; use the current checkout's copy.
    shutil.copyfile(repo_root / "llm_backend/judge_bridge.py", logs / "judge_bridge.py")
    runner = ContainerRunner(
        ContainerConfig(
            network_mode="bridge",
            internet_policy=InternetPolicy.from_mode("open"),
            logs_path=logs,
            env_vars=env,
            forward_host_credentials=False,
            codex_auth="shared" if backend == "codex" else "none",
            published_ports=[f"127.0.0.1::{BRIDGE_PORT}"],
        )
    )
    driver = f"python /logs/judge_bridge.py --backend {backend} --host 0.0.0.0 --port {BRIDGE_PORT}"
    command = runner.build_composite_command(registration.install_script, registration.agent_version, driver)
    request = ExecInput(command=command, label=f"judge-{backend}")
    previous_url = os.environ.get("SREGYM_JUDGE_BRIDGE_URL")
    proc = None
    try:
        if force_build:
            runner.build_image()
        else:
            runner.ensure_image_exists()
        with (logs / "container.log").open("w") as output:
            proc = subprocess.Popen(runner.build_docker_command(request), stdout=output, stderr=subprocess.STDOUT)
            url = _wait_for_bridge(proc, request.container_name, logs / "container.log")
            os.environ["SREGYM_JUDGE_BRIDGE_URL"] = url
            logger.info("Using %s for the judge; CLI logs: %s", backend, logs)
            yield
    finally:
        if previous_url is None:
            os.environ.pop("SREGYM_JUDGE_BRIDGE_URL", None)
        else:
            os.environ["SREGYM_JUDGE_BRIDGE_URL"] = previous_url
        try:
            if request.container_name:
                runner.stop_container(request.container_name)
            if proc is not None:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        finally:
            runner.cleanup_credential_tmps()
