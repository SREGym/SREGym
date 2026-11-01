"""Claude Code agent with the Radar MCP server attached.

Identical to clients.claudecode.claudecode_agent.ClaudeCodeAgent except that
the CLI is started with:
  --mcp-config      Radar's streamable-HTTP MCP endpoint (host-side daemon)
  --strict-mcp-config
  --append-system-prompt  routing.md (this environment has no kubectl); Radar's
                          usage guidance arrives as Radar's MCP server instructions
  --permission-mode dontAsk  run exactly the allowlist, no auto-mode classifier
and `mcp__radar` plus EXTRA_TOOLS are added to the allowed tools.

Radar is the agent's only path to the cluster: kubectl is removed from the
container at start, and the registration's cluster_access: false gives the
container no kubeconfig and no egress to the API proxy or SREGym's MCP server.

Radar runs on the host and reads the cluster through a long-lived instance of
SREGym's own filtering proxy (sregym/service/k8s_proxy.py), so it sees the same
filtered cluster view an agent's kubectl would.
"""

import json
import logging
import os
import subprocess
from pathlib import Path

from clients.claudecode.claudecode_agent import ClaudeCodeAgent

logger = logging.getLogger("all.claudecode_radar.agent")

ROUTING_PATH = Path(__file__).with_name("routing.md")
AGENT_KUBECONFIG = Path("/root/.kube/config")


def remove_cluster_access_from_container() -> None:
    """Delete kubectl and refuse to run if the agent still has cluster credentials.

    The credentials and the API route are the harness's to withhold (cluster_access: false in the
    registration); this check makes a misconfigured registration fail loudly instead of quietly
    handing the agent a working kubeconfig.
    """
    if os.environ.get("KUBECONFIG") or AGENT_KUBECONFIG.exists():
        raise RuntimeError(
            "claudecode_radar needs an agent container without cluster credentials: set "
            "cluster_access: false on this agent's registration in agents.yaml"
        )
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / "kubectl"
        if candidate.is_file():
            candidate.unlink()
            logger.info(f"removed {candidate}")


def radar_mcp_url() -> str:
    return os.environ.get("RADAR_MCP_URL", "http://host.docker.internal:9283/mcp")


# Tools the CLI ships beyond the stock list that an investigation uses: ToolSearch loads
# deferred MCP tool schemas (Radar's tools are deferred), the rest are background-task
# and MCP-resource helpers.
EXTRA_TOOLS = [
    "ToolSearch",
    "Monitor",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskStop",
    "TaskUpdate",
    "ListMcpResourcesTool",
    "ReadMcpResourceTool",
    "ReadMcpResourceDirTool",
]


class RadarClaudeCodeAgent(ClaudeCodeAgent):
    @classmethod
    def allowed_tools(cls) -> list[str]:
        return [*super().allowed_tools(), *EXTRA_TOOLS, "mcp__radar"]

    def _write_mcp_config(self) -> Path:
        path = self.logs_dir / "radar-mcp-config.json"
        path.write_text(json.dumps({"mcpServers": {"radar": {"type": "http", "url": radar_mcp_url()}}}, indent=2))
        return path

    def run(self, instruction: str) -> int:
        # Mirrors ClaudeCodeAgent.run; the only additions are marked RADAR.
        model = self.model_name.split("/")[-1]
        invalid_patterns = ["bedrock", "litellm", "azure", "openai", "watsonx", "gemini"]
        if any(pattern in model.lower() for pattern in invalid_patterns):
            logger.warning(
                f"Model '{model}' appears to be for a non-Anthropic provider. Defaulting to 'sonnet' for Claude Code."
            )
            model = "sonnet"

        logger.info(f"Running Claude Code (Radar MCP) with instruction: {instruction}")
        logger.info(f"Using model: {model}")

        self._setup_sessions_structure()

        env = os.environ.copy()
        env["CLAUDE_CONFIG_DIR"] = str(self.sessions_dir)
        env["FORCE_AUTO_BACKGROUND_TASKS"] = "1"
        env["ENABLE_BACKGROUND_TASKS"] = "1"

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
        if not api_key and not oauth_token:
            logger.error("No Anthropic API authentication found (ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN)")
            return 1
        if oauth_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
            env.pop("ANTHROPIC_API_KEY", None)
        elif api_key:
            env["ANTHROPIC_API_KEY"] = api_key

        env["ANTHROPIC_MODEL"] = model
        reasoning_effort = os.environ.get("AGENT_REASONING_EFFORT")
        if "MAX_THINKING_TOKENS" in os.environ:
            env["MAX_THINKING_TOKENS"] = os.environ["MAX_THINKING_TOKENS"]

        # RADAR: MCP server config and the note that this environment has no kubectl.
        mcp_config = self._write_mcp_config()
        remove_cluster_access_from_container()
        routing = ROUTING_PATH.read_text()

        command = [
            "claude",
            "--verbose",
            "--output-format",
            "stream-json",
            "--thinking-display",
            "summarized",
            "-p",
            instruction,
        ]
        if reasoning_effort:
            command.extend(["--effort", reasoning_effort])
        # RADAR
        command.extend(["--mcp-config", str(mcp_config), "--strict-mcp-config"])
        command.extend(["--append-system-prompt", routing])
        # The CLI defaults to auto mode on some accounts, where a classifier can refuse
        # the fix itself (e.g. deleting a webhook). dontAsk runs exactly the allowlist.
        command.extend(["--permission-mode", "dontAsk"])
        command.extend(["--allowedTools", *self.allowed_tools()])

        logger.info(f"Executing command: {' '.join(c if c != routing else '<routing.md>' for c in command)}")

        try:
            with open(self.output_path, "w") as out_file:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=env,
                    text=True,
                    bufsize=1,
                )
                for line in process.stdout:
                    out_file.write(line)
                    out_file.flush()
                    print(line, end="", flush=True)
                process.wait()

            logger.info(f"Claude Code finished with return code: {process.returncode}")
            return process.returncode
        except Exception as e:
            logger.error(f"Error running Claude Code: {e}")
            raise
