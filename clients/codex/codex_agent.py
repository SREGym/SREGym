"""
Codex agent implementation for SREGym.
Based on Harbor's Codex agent implementation for parity experiments.
"""

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("all.codex.agent")


class CodexAgent:
    """
    The Codex agent uses OpenAI's Codex CLI tool to solve tasks.

    This implementation closely mirrors Harbor's Codex agent for parity experiments.
    """

    _OUTPUT_FILENAME = "codex.txt"

    @staticmethod
    def check_installation() -> bool:
        """
        Check if Codex CLI is installed.

        Returns:
            True if codex is available, False otherwise
        """
        return shutil.which("codex") is not None

    @staticmethod
    def ensure_installed(auto_install: bool = True) -> None:
        """
        Ensure Codex CLI is installed, optionally attempting installation.

        Args:
            auto_install: If True, attempt to install codex if not found

        Raises:
            RuntimeError: If codex is not installed and auto_install fails
        """
        if CodexAgent.check_installation():
            logger.info("Codex CLI is already installed")
            return

        logger.warning("Codex CLI not found in PATH")

        if not auto_install:
            raise RuntimeError(
                "Codex CLI is not installed. Please install it using:\n"
                "  pip install codex-cli\n"
                "Or visit: https://github.com/anthropics/codex"
            )

        # Attempt auto-installation
        logger.info("Attempting to install Codex CLI via pip...")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "codex-cli"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            logger.info("Successfully installed Codex CLI")

            # Verify installation
            if not CodexAgent.check_installation():
                raise RuntimeError("Codex CLI installation appeared to succeed but command is still not available")

        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"Failed to auto-install Codex CLI: {e}\n"
                "Please install it manually using:\n"
                "  pip install codex-cli\n"
                "Or visit: https://github.com/anthropics/codex"
            )

    def __init__(
        self,
        logs_dir: Path,
        model_name: str,
        codex_home: Optional[Path] = None,
    ):
        """
        Initialize the Codex agent.

        Args:
            logs_dir: Directory to store logs and output
            model_name: Model name to use (e.g., "claude-sonnet-4-5")
            codex_home: Directory for Codex configuration (defaults to logs_dir)
        """
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        self.model_name = model_name
        self.codex_home = Path(codex_home) if codex_home else self.logs_dir
        self.codex_home.mkdir(parents=True, exist_ok=True)

        logger.info(f"Initialized Codex agent with model={model_name}")
        logger.info(f"Logs dir: {self.logs_dir}")
        logger.info(f"Codex home: {self.codex_home}")

    @property
    def output_path(self) -> Path:
        """Path to Codex output file."""
        return self.logs_dir / self._OUTPUT_FILENAME

    @property
    def trajectory_path(self) -> Path:
        """Path to trajectory JSON file."""
        return self.logs_dir / "trajectory.json"

    @staticmethod
    def _extract_message_text(content: list[Any]) -> str:
        """Extract joined text from Codex content blocks."""
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)

    @staticmethod
    def _parse_output_blob(raw: Any) -> tuple[str | None, dict[str, Any] | None]:
        """Extract textual output and metadata from Codex tool outputs."""
        if raw is None:
            return None, None

        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return raw, None
        else:
            parsed = raw

        if isinstance(parsed, dict):
            output = parsed.get("output")
            if output is None and parsed:
                # dumping remaining structure if output missing
                output = json.dumps(parsed, ensure_ascii=False)
            metadata = parsed.get("metadata")
            return output, metadata if isinstance(metadata, dict) else None

        return str(parsed), None

    def get_usage_metrics(self) -> dict[str, int]:
        """
        Extract usage metrics from Codex output.

        Returns:
            Dictionary with keys: input_tokens, cached_input_tokens, output_tokens
        """
        metrics = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
        }

        if not self.output_path.exists():
            logger.debug(f"Codex output file {self.output_path} does not exist")
            return metrics

        with open(self.output_path) as f:
            lines = f.readlines()

        # Parse from the end to get the most recent usage info
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue

            try:
                parsed = json.loads(line)

                if isinstance(parsed, dict) and "usage" in parsed:
                    usage = parsed["usage"]
                    metrics["input_tokens"] = usage.get("input_tokens", 0)
                    metrics["cached_input_tokens"] = usage.get("cached_input_tokens", 0)
                    metrics["output_tokens"] = usage.get("output_tokens", 0)
                    logger.info(f"Extracted usage metrics: {metrics}")
                    return metrics

            except json.JSONDecodeError:
                continue

        return metrics

    def _setup_auth(self) -> None:
        """Create auth.json file for Codex."""
        auth_file = self.codex_home / "auth.json"

        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            logger.warning("OPENAI_API_KEY not set in environment")

        auth_data = {"OPENAI_API_KEY": api_key}

        with open(auth_file, "w") as f:
            json.dump(auth_data, f)

        logger.info(f"Created auth file at {auth_file}")

    def _cleanup_auth(self) -> None:
        """Remove auth.json file after execution."""
        auth_file = self.codex_home / "auth.json"
        if auth_file.exists():
            auth_file.unlink()
            logger.info(f"Removed auth file at {auth_file}")

    def run(self, instruction: str) -> int:
        """
        Run the Codex agent with the given instruction.

        Args:
            instruction: The task instruction to pass to Codex

        Returns:
            Return code from Codex execution (0 for success)
        """
        # Extract model name (remove provider prefix if present)
        model = self.model_name.split("/")[-1]

        logger.info(f"Running Codex with instruction: {instruction}")
        logger.info(f"Using model: {model}")

        # Setup authentication
        self._setup_auth()

        try:
            # Build Codex command
            command = [
                "codex",
                "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check",
                "--model",
                model,
                "--json",
                "--enable",
                "unified_exec",
                "-c",
                "model_reasoning_effort=high",
                "--",  # end of flags
                instruction,
            ]

            logger.info(f"Executing command: {' '.join(command)}")

            # Set environment variables
            env = os.environ.copy()
            env["CODEX_HOME"] = str(self.codex_home)

            # Run Codex and capture output
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

                # Stream output to both file and logger
                for line in process.stdout:
                    out_file.write(line)
                    out_file.flush()
                    # Also log to console (strip to avoid double newlines)
                    print(line, end="", flush=True)

                process.wait()

            logger.info(f"Codex finished with return code: {process.returncode}")
            return process.returncode

        finally:
            # Always cleanup auth file
            self._cleanup_auth()
