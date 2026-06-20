"""
GitHub Copilot CLI agent implementation for SREGym.
"""

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger("all.copilot.agent")


class CopilotCliAgent:
    """
    The Copilot CLI agent uses GitHub's Copilot CLI tool to solve tasks.
    """

    _OUTPUT_FILENAME = "copilot-cli.txt"

    @staticmethod
    def check_installation() -> bool:
        """Check if Copilot CLI is installed."""
        return shutil.which("copilot") is not None

    @staticmethod
    def ensure_installed(auto_install: bool = True) -> None:
        """
        Ensure Copilot CLI is installed, optionally attempting installation.

        Raises:
            RuntimeError: If copilot is not installed and auto_install fails
        """
        if CopilotCliAgent.check_installation():
            logger.info("Copilot CLI is already installed")
            return

        logger.warning("Copilot CLI not found in PATH")

        if not auto_install:
            raise RuntimeError(
                "Copilot CLI is not installed. Please install it using:\n"
                "  npm install -g @github/copilot\n"
                "Or visit: https://docs.github.com/en/copilot/how-tos/copilot-cli"
            )

        logger.info("Attempting to install Copilot CLI via npm...")
        try:
            subprocess.check_call(
                ["npm", "install", "-g", "@github/copilot"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            logger.info("Successfully installed Copilot CLI")

            if not CopilotCliAgent.check_installation():
                raise RuntimeError("Copilot CLI installation appeared to succeed but command is still not available")

        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            error_msg = f"Failed to auto-install Copilot CLI: {e}\n"
            if isinstance(e, FileNotFoundError):
                error_msg += "npm is not installed. Please install Node.js and npm first.\n"
            error_msg += (
                "Please install Copilot CLI manually using:\n"
                "  npm install -g @github/copilot\n"
                "Or visit: https://docs.github.com/en/copilot/how-tos/copilot-cli"
            )
            raise RuntimeError(error_msg) from None

    def __init__(
        self,
        logs_dir: Path,
        model_name: str,
        copilot_home: Path | None = None,
    ):
        """
        Initialize the Copilot CLI agent.

        Args:
            logs_dir: Directory to store logs and output
            model_name: Model name to use (e.g., "gpt-4.1")
            copilot_home: Directory for Copilot configuration (defaults to ~/.copilot)
        """
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        self.model_name = model_name
        self.copilot_home = Path(copilot_home) if copilot_home else Path.home() / ".copilot"

        logger.info(f"Initialized Copilot CLI agent with model={model_name}")
        logger.info(f"Logs dir: {self.logs_dir}")
        logger.info(f"Copilot home: {self.copilot_home}")

    @property
    def output_path(self) -> Path:
        """Path to Copilot CLI output file."""
        return self.logs_dir / self._OUTPUT_FILENAME

    @property
    def otel_dir(self) -> Path:
        """Path to OTel JSONL directory."""
        return self.logs_dir / "otel"

    @property
    def transcript_path(self) -> Path:
        """Path to session transcript markdown."""
        return self.logs_dir / "copilot-session.md"

    def get_usage_metrics(self) -> dict[str, int]:
        """
        Extract usage metrics from Copilot CLI OTel JSONL files.

        Returns:
            Dictionary with keys: input_tokens, cached_input_tokens, output_tokens
        """
        metrics = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
        }

        if not self.otel_dir.exists():
            logger.debug("No OTel directory found for metrics")
            return metrics

        otel_files = list(self.otel_dir.glob("*.jsonl"))
        if not otel_files:
            logger.debug(f"No OTel JSONL files found in {self.otel_dir}")
            return metrics

        total_input = 0
        total_output = 0
        total_cached = 0

        for otel_file in otel_files:
            with open(otel_file) as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        event = json.loads(stripped)
                        attrs = event.get("attributes", {})
                        # OTel semantic conventions for GenAI
                        total_input += attrs.get("gen_ai.usage.input_tokens", 0)
                        total_output += attrs.get("gen_ai.usage.output_tokens", 0)
                        total_cached += attrs.get("gen_ai.usage.cache_read_input_tokens", 0)
                    except json.JSONDecodeError:
                        continue

        metrics["input_tokens"] = total_input
        metrics["output_tokens"] = total_output
        metrics["cached_input_tokens"] = total_cached

        logger.info(f"Extracted usage metrics: {metrics}")
        return metrics

    def run(self, instruction: str) -> int:
        """
        Run the Copilot CLI agent with the given instruction.

        Args:
            instruction: The task instruction to pass to Copilot CLI

        Returns:
            Return code from Copilot CLI execution (0 for success)
        """
        model = self.model_name.split("/")[-1]

        logger.info(f"Running Copilot CLI with model: {model}")

        # Setup OTel directory for metrics capture
        self.otel_dir.mkdir(parents=True, exist_ok=True)
        otel_path = self.otel_dir / "copilot-otel.jsonl"

        # Build environment variables
        env = os.environ.copy()
        env["COPILOT_HOME"] = str(self.copilot_home)
        env["COPILOT_MODEL"] = model

        # Enable OTel file export for token metrics
        env["COPILOT_OTEL_ENABLED"] = "true"
        env["COPILOT_OTEL_EXPORTER_TYPE"] = "file"
        env["COPILOT_OTEL_FILE_EXPORTER_PATH"] = str(otel_path)

        # Auth: BYOK mode (COPILOT_PROVIDER_BASE_URL) or GitHub-hosted
        # (COPILOT_GITHUB_TOKEN > GH_TOKEN > GITHUB_TOKEN)
        byok_vars = ["COPILOT_PROVIDER_BASE_URL", "COPILOT_PROVIDER_API_KEY", "COPILOT_PROVIDER_TYPE"]
        has_byok = bool(os.environ.get("COPILOT_PROVIDER_BASE_URL"))

        token_vars = ["COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"]
        has_github_auth = any(os.environ.get(v) for v in token_vars)

        if not has_byok and not has_github_auth:
            logger.error("=" * 80)
            logger.error("ERROR: No authentication found for Copilot CLI")
            logger.error("Option 1 - GitHub-hosted (set one of):")
            logger.error("  - COPILOT_GITHUB_TOKEN (fine-grained PAT with Copilot Requests permission)")
            logger.error("  - GH_TOKEN (GitHub CLI OAuth token)")
            logger.error("  - GITHUB_TOKEN")
            logger.error("Option 2 - BYOK (set all relevant):")
            logger.error("  - COPILOT_PROVIDER_BASE_URL (required)")
            logger.error("  - COPILOT_PROVIDER_API_KEY (required for remote providers)")
            logger.error("  - COPILOT_PROVIDER_TYPE (openai|azure|anthropic, default: openai)")
            logger.error("=" * 80)
            return 1

        # Pass through BYOK env vars if set
        for var in byok_vars:
            if os.environ.get(var):
                env[var] = os.environ[var]

        # Build command
        command = [
            "copilot",
            "-p",
            instruction,
            "--allow-all",
            "--no-ask-user",
            "--model",
            model,
            f"--share={self.transcript_path}",
        ]

        logger.info(f"Executing command: copilot -p <instruction> --allow-all --no-ask-user --model {model}")

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

                if process.stdout:
                    for line in process.stdout:
                        out_file.write(line)
                        out_file.flush()
                        print(line, end="", flush=True)

                process.wait()

            logger.info(f"Copilot CLI finished with return code: {process.returncode}")
            return process.returncode

        except Exception as e:
            logger.error(f"Error running Copilot CLI: {e}")
            raise
