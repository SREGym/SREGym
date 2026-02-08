"""
OpenCode agent implementation for SREGym.
Based on Harbor's OpenCode agent implementation for parity experiments.
"""

import json
import logging
import os
import shlex
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("all.opencode.agent")


# Provider to environment variable mapping
PROVIDER_ENV_VARS: dict[str, list[str]] = {
    "amazon-bedrock": ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION"],
    "anthropic": ["ANTHROPIC_API_KEY"],
    "azure": ["AZURE_RESOURCE_NAME", "AZURE_API_KEY"],
    "deepseek": ["DEEPSEEK_API_KEY"],
    "github-copilot": ["GITHUB_TOKEN"],
    "google": [
        "GEMINI_API_KEY",
        "GOOGLE_GENERATIVE_AI_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GOOGLE_API_KEY",
    ],
    "groq": ["GROQ_API_KEY"],
    "huggingface": ["HF_TOKEN"],
    "llama": ["LLAMA_API_KEY"],
    "mistral": ["MISTRAL_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "xai": ["XAI_API_KEY"],
    # OpenCode native providers (no API key needed - uses opencode.ai)
    "opencode": [],
    "zai-coding-plan": [],
}


class OpenCodeAgent:
    """
    The OpenCode agent uses the opencode-ai tool to solve tasks.

    This implementation closely mirrors Harbor's OpenCode agent for parity experiments.
    """

    _OUTPUT_FILENAME = "opencode.txt"

    @staticmethod
    def check_installation() -> bool:
        """Check if OpenCode CLI is installed."""
        return shutil.which("opencode") is not None

    @staticmethod
    def ensure_installed(auto_install: bool = True) -> None:
        """
        Ensure OpenCode CLI is installed, optionally attempting installation.

        Args:
            auto_install: If True, attempt to install opencode if not found

        Raises:
            RuntimeError: If opencode is not installed and auto_install fails
        """
        if OpenCodeAgent.check_installation():
            logger.info("OpenCode CLI is already installed")
            return

        logger.warning("OpenCode CLI not found in PATH")

        if not auto_install:
            raise RuntimeError(
                "OpenCode CLI is not installed. Please install it using:\n"
                "  npm install -g opencode-ai\n"
                "Or visit: https://github.com/opencode-ai/opencode"
            )

        logger.info("Attempting to install OpenCode CLI via npm...")
        try:
            subprocess.check_call(
                ["npm", "install", "-g", "opencode-ai"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            logger.info("Successfully installed OpenCode CLI")

            if not OpenCodeAgent.check_installation():
                raise RuntimeError("OpenCode CLI installation appeared to succeed but command is still not available")

        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            error_msg = f"Failed to auto-install OpenCode CLI: {e}\n"
            if isinstance(e, FileNotFoundError):
                error_msg += "npm is not installed. Please install Node.js and npm first.\n"
            error_msg += (
                "Please install OpenCode CLI manually using:\n"
                "  npm install -g opencode-ai\n"
                "Or visit: https://github.com/opencode-ai/opencode"
            )
            raise RuntimeError(error_msg)

    @staticmethod
    def get_provider_from_model(model_name: str) -> str:
        """Extract provider from model name (format: provider/model)."""
        if "/" not in model_name:
            raise ValueError(f"Model name must be in format 'provider/model_name', got: {model_name}")
        return model_name.split("/", 1)[0]

    @staticmethod
    def get_supported_providers() -> list[str]:
        """Return list of supported providers."""
        return list(PROVIDER_ENV_VARS.keys())

    def __init__(
        self,
        logs_dir: Path,
        model_name: str,
    ):
        """
        Initialize the OpenCode agent.

        Args:
            logs_dir: Directory to store logs and output
            model_name: Model name in format 'provider/model' (e.g., "anthropic/claude-sonnet-4-5")
        """
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        self.model_name = model_name
        self.provider = self.get_provider_from_model(model_name)

        if self.provider not in PROVIDER_ENV_VARS:
            raise ValueError(
                f"Unknown provider '{self.provider}'. Supported providers: {self.get_supported_providers()}"
            )

        logger.info(f"Initialized OpenCode agent with model={model_name}")
        logger.info(f"Provider: {self.provider}")
        logger.info(f"Logs dir: {self.logs_dir}")

    @property
    def output_path(self) -> Path:
        """Path to OpenCode output file."""
        return self.logs_dir / self._OUTPUT_FILENAME

    @property
    def trajectory_path(self) -> Path:
        """Path to trajectory JSON file."""
        return self.logs_dir / "trajectory.json"

    @property
    def sessions_dir(self) -> Path:
        """Path to sessions directory (similar to Codex)."""
        return self.logs_dir / "sessions"

    def _get_session_id(self) -> str | None:
        """Extract session ID from OpenCode output."""
        if not self.output_path.exists():
            return None

        try:
            with open(self.output_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        if isinstance(data, dict) and "sessionID" in data:
                            return data["sessionID"]
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            logger.warning(f"Error extracting session ID: {e}")

        return None

    def export_session(self) -> Path | None:
        """
        Export the session to a structured JSON file.

        Uses OpenCode's native export command to save session data
        in a format similar to Codex's session storage.

        Returns:
            Path to exported session file, or None if export failed
        """
        session_id = self._get_session_id()
        if not session_id:
            logger.warning("No session ID found in output, cannot export session")
            return None

        # Create sessions directory with date structure (like Codex)
        now = datetime.now()
        session_subdir = self.sessions_dir / str(now.year) / f"{now.month:02d}" / f"{now.day:02d}"
        session_subdir.mkdir(parents=True, exist_ok=True)

        # Export session using OpenCode CLI
        session_file = session_subdir / f"session-{session_id}.json"
        temp_file = session_subdir / f".tmp-{session_id}.json"

        try:
            # Write directly to temp file to handle large outputs
            with open(temp_file, "w") as f:
                result = subprocess.run(
                    ["opencode", "export", session_id],
                    stdout=f,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=60,
                )

            if result.returncode != 0:
                logger.warning(f"Failed to export session: {result.stderr}")
                temp_file.unlink(missing_ok=True)
                return None

            # Read and parse the temp file
            with open(temp_file) as f:
                content = f.read()

            # Skip the "Exporting session: ..." line
            json_start = content.find("{")
            if json_start == -1:
                logger.warning("No JSON found in export output")
                temp_file.unlink(missing_ok=True)
                return None

            json_data = content[json_start:]

            # Validate and reformat JSON
            parsed = json.loads(json_data)
            with open(session_file, "w") as f:
                json.dump(parsed, f, indent=2)

            # Clean up temp file
            temp_file.unlink(missing_ok=True)

            logger.info(f"Exported session to {session_file}")
            return session_file

        except subprocess.TimeoutExpired:
            logger.warning("Session export timed out")
            temp_file.unlink(missing_ok=True)
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse exported session JSON: {e}")
            temp_file.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"Error exporting session: {e}")
            temp_file.unlink(missing_ok=True)

        return None

    def get_usage_metrics(self) -> dict[str, int]:
        """
        Extract usage metrics from OpenCode output.

        Returns:
            Dictionary with keys: input_tokens, cached_input_tokens, output_tokens
        """
        metrics = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
        }

        if not self.output_path.exists():
            logger.debug(f"OpenCode output file {self.output_path} does not exist")
            return metrics

        total_input = 0
        total_output = 0
        total_cached = 0

        try:
            with open(self.output_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        if not isinstance(data, dict):
                            continue

                        # OpenCode format: step_finish events have tokens
                        if data.get("type") == "step_finish":
                            part = data.get("part", {})
                            tokens = part.get("tokens", {})
                            if tokens:
                                total_input += tokens.get("input", 0)
                                total_output += tokens.get("output", 0) + tokens.get("reasoning", 0)
                                cache = tokens.get("cache", {})
                                total_cached += cache.get("read", 0)

                        # Also check for standard usage format
                        if "usage" in data:
                            usage = data["usage"]
                            total_input += usage.get("input_tokens", 0)
                            total_output += usage.get("output_tokens", 0)
                            total_cached += usage.get("cached_input_tokens", 0)

                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            logger.warning(f"Error parsing OpenCode output for metrics: {e}")

        metrics["input_tokens"] = total_input
        metrics["output_tokens"] = total_output
        metrics["cached_input_tokens"] = total_cached

        logger.info(f"Extracted usage metrics: {metrics}")
        return metrics

    def _build_env(self) -> dict[str, str]:
        """Build environment variables for OpenCode execution."""
        env = os.environ.copy()

        # Get provider-specific environment variables
        env_vars = PROVIDER_ENV_VARS.get(self.provider, [])
        has_auth = False

        for var in env_vars:
            if var in os.environ:
                env[var] = os.environ[var]
                has_auth = True

        if not has_auth:
            logger.warning(f"No authentication found for provider '{self.provider}'. Expected one of: {env_vars}")

        # Enable fake VCS for OpenCode (required for non-git directories)
        env["OPENCODE_FAKE_VCS"] = "git"

        return env

    def run(self, instruction: str, export_session: bool = True) -> int:
        """
        Run the OpenCode agent with the given instruction.

        Args:
            instruction: The task instruction to pass to OpenCode
            export_session: If True, export session to JSON after completion

        Returns:
            Return code from OpenCode execution (0 for success)
        """
        logger.info(f"Running OpenCode with instruction: {instruction}")
        logger.info(f"Using model: {self.model_name}")

        env = self._build_env()

        # Build command
        escaped_instruction = shlex.quote(instruction)
        command = f"opencode --model {self.model_name} run --format=json {escaped_instruction}"

        logger.info(f"Executing command: {command}")

        try:
            with open(self.output_path, "w") as out_file:
                process = subprocess.Popen(
                    command,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=env,
                    text=True,
                    bufsize=1,
                )

                if process.stdout:
                    for line in process.stdout:
                        out_file.write(line)
                        out_file.flush()
                        print(line, end="", flush=True)

                process.wait()

            logger.info(f"OpenCode finished with return code: {process.returncode}")

            # Export session for structured storage (like Codex)
            if export_session:
                session_path = self.export_session()
                if session_path:
                    logger.info(f"Session exported to: {session_path}")

            return process.returncode

        except Exception as e:
            logger.error(f"Error running OpenCode: {e}")
            raise
