"""
Gemini CLI agent implementation for SREGym.
Based on Harbor's Gemini CLI agent implementation for parity experiments.
"""

import json
import logging
import os
import shlex
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("all.geminicli.agent")


class GeminiCliAgent:
    """
    The Gemini CLI agent uses Google's Gemini CLI tool to solve tasks.

    This implementation closely mirrors Harbor's Gemini CLI agent for parity experiments.
    """

    _OUTPUT_FILENAME = "gemini-cli.txt"

    @staticmethod
    def check_installation() -> bool:
        """Check if Gemini CLI is installed."""
        return shutil.which("gemini") is not None

    @staticmethod
    def ensure_installed(auto_install: bool = True) -> None:
        """
        Ensure Gemini CLI is installed, optionally attempting installation.

        Args:
            auto_install: If True, attempt to install gemini if not found

        Raises:
            RuntimeError: If gemini is not installed and auto_install fails
        """
        if GeminiCliAgent.check_installation():
            logger.info("Gemini CLI is already installed")
            return

        logger.warning("Gemini CLI not found in PATH")

        if not auto_install:
            raise RuntimeError(
                "Gemini CLI is not installed. Please install it using:\n"
                "  npm install -g @google/gemini-cli\n"
                "Or visit: https://github.com/google-gemini/gemini-cli"
            )

        logger.info("Attempting to install Gemini CLI via npm...")
        try:
            subprocess.check_call(
                ["npm", "install", "-g", "@google/gemini-cli"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            logger.info("Successfully installed Gemini CLI")

            if not GeminiCliAgent.check_installation():
                raise RuntimeError("Gemini CLI installation appeared to succeed but command is still not available")

        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            error_msg = f"Failed to auto-install Gemini CLI: {e}\n"
            if isinstance(e, FileNotFoundError):
                error_msg += "npm is not installed. Please install Node.js and npm first.\n"
            error_msg += (
                "Please install Gemini CLI manually using:\n"
                "  npm install -g @google/gemini-cli\n"
                "Or visit: https://github.com/google-gemini/gemini-cli"
            )
            raise RuntimeError(error_msg) from e

    def __init__(
        self,
        logs_dir: Path,
        model_name: str,
        gemini_home: Path | None = None,
    ):
        """
        Initialize the Gemini CLI agent.

        Args:
            logs_dir: Directory to store logs and output
            model_name: Model name to use (e.g., "gemini-2.0-flash")
            gemini_home: Directory for Gemini configuration (defaults to ~/.gemini)
        """
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        self.model_name = model_name
        self.gemini_home = Path(gemini_home) if gemini_home else Path.home() / ".gemini"

        logger.info(f"Initialized Gemini CLI agent with model={model_name}")
        logger.info(f"Logs dir: {self.logs_dir}")
        logger.info(f"Gemini home: {self.gemini_home}")

    @property
    def output_path(self) -> Path:
        """Path to Gemini CLI output file."""
        return self.logs_dir / self._OUTPUT_FILENAME

    @property
    def sessions_dir(self) -> Path:
        """Path to sessions directory."""
        return self.logs_dir / "sessions"

    def _find_session_file(self) -> Path | None:
        """Find the most recent Gemini session file."""
        tmp_dir = self.gemini_home / "tmp"
        if not tmp_dir.exists():
            return None

        # Gemini stores sessions in: ~/.gemini/tmp/*/chats/session-*.json
        session_files = list(tmp_dir.glob("*/chats/session-*.json"))
        if not session_files:
            # Fallback to old location
            session_files = list(tmp_dir.glob("session-*.json"))
        if not session_files:
            return None

        # Return the most recently modified session file
        return max(session_files, key=lambda p: p.stat().st_mtime)

    def _archive_session(self) -> Path | None:
        """Archive the Gemini session file to sessions directory."""
        session_file = self._find_session_file()
        if not session_file or not session_file.exists():
            logger.warning("No session file found to copy")
            return None

        try:
            # Save to sessions directory with date structure (like Codex/OpenCode)
            now = datetime.now()
            session_subdir = self.sessions_dir / str(now.year) / f"{now.month:02d}" / f"{now.day:02d}"
            session_subdir.mkdir(parents=True, exist_ok=True)

            # Extract session ID from filename (session-2026-02-04T16-12-e580aecd.json -> e580aecd)
            session_id = session_file.stem.split("-")[-1] if "-" in session_file.stem else session_file.stem
            archived_path = session_subdir / f"session-{session_id}.json"
            shutil.copy(session_file, archived_path)
            logger.info(f"Archived session to {archived_path}")

            return archived_path
        except Exception as e:
            logger.warning(f"Could not copy session file: {e}")
            return None

    def get_usage_metrics(self) -> dict[str, int]:
        """
        Extract usage metrics from Gemini CLI session file.

        Returns:
            Dictionary with keys: input_tokens, cached_input_tokens, output_tokens
        """
        metrics = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
        }

        # Read directly from the session file
        session_file = self._find_session_file()
        if not session_file or not session_file.exists():
            logger.debug("No session file found for metrics")
            return metrics

        try:
            trajectory = json.loads(session_file.read_text())
        except Exception as e:
            logger.warning(f"Error loading session: {e}")
            return metrics

        total_input = 0
        total_output = 0
        total_cached = 0

        for message in trajectory.get("messages", []):
            if message.get("type") == "gemini":
                tokens = message.get("tokens", {})
                total_input += tokens.get("input", 0)
                # output includes: output + thoughts + tool tokens
                total_output += tokens.get("output", 0) + tokens.get("thoughts", 0) + tokens.get("tool", 0)
                total_cached += tokens.get("cached", 0)

        metrics["input_tokens"] = total_input
        metrics["output_tokens"] = total_output
        metrics["cached_input_tokens"] = total_cached

        logger.info(f"Extracted usage metrics: {metrics}")
        return metrics

    def run(self, instruction: str) -> int:
        """
        Run the Gemini CLI agent with the given instruction.

        Args:
            instruction: The task instruction to pass to Gemini CLI

        Returns:
            Return code from Gemini CLI execution (0 for success)
        """
        # Extract model name (remove provider prefix if present)
        model = self.model_name.split("/")[-1]

        logger.info(f"Running Gemini CLI with instruction: {instruction}")
        logger.info(f"Using model: {model}")

        # Build environment variables
        env = os.environ.copy()

        # Auth environment variables
        auth_vars = [
            "GEMINI_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_LOCATION",
            "GOOGLE_GENAI_USE_VERTEXAI",
            "GOOGLE_API_KEY",
        ]

        has_auth = False
        for var in auth_vars:
            if var in os.environ:
                env[var] = os.environ[var]
                has_auth = True

        if not has_auth:
            logger.error("=" * 80)
            logger.error("ERROR: No Google/Gemini API authentication found")
            logger.error("Please set one of the following environment variables:")
            logger.error("  - GEMINI_API_KEY")
            logger.error("  - GOOGLE_API_KEY")
            logger.error("  - GOOGLE_APPLICATION_CREDENTIALS (for service account)")
            logger.error("=" * 80)
            return 1

        # Build command
        escaped_instruction = shlex.quote(instruction)
        command = f"gemini -p {escaped_instruction} -y -m {model}"

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

            logger.info(f"Gemini CLI finished with return code: {process.returncode}")

            # Archive session file after execution
            self._archive_session()

            return process.returncode

        except Exception as e:
            logger.error(f"Error running Gemini CLI: {e}")
            raise
