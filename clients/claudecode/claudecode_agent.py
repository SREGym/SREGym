"""
Claude Code agent implementation for SREGym.
Based on Harbor's Claude Code agent implementation for parity experiments.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("all.claudecode.agent")


class ClaudeCodeAgent:
    """
    The Claude Code agent uses Claude's CLI tool to solve tasks.

    This implementation closely mirrors Harbor's Claude Code agent for parity experiments.
    """

    _OUTPUT_FILENAME = "claude-code.txt"

    @staticmethod
    def check_installation() -> bool:
        """
        Check if Claude Code CLI is installed.

        Returns:
            True if claude is available, False otherwise
        """
        return shutil.which("claude") is not None

    @staticmethod
    def ensure_installed(auto_install: bool = True) -> None:
        """
        Ensure Claude Code CLI is installed, optionally attempting installation.

        Args:
            auto_install: If True, attempt to install claude if not found

        Raises:
            RuntimeError: If claude is not installed and auto_install fails
        """
        if ClaudeCodeAgent.check_installation():
            logger.info("Claude Code CLI is already installed")
            return

        logger.warning("Claude Code CLI not found in PATH")

        if not auto_install:
            raise RuntimeError(
                "Claude Code CLI is not installed. Please install it using:\n"
                "  npm install -g @anthropic-ai/claude-code\n"
                "Or visit: https://docs.claude.ai/claude-code"
            )

        # Attempt auto-installation
        logger.info("Attempting to install Claude Code CLI via npm...")
        try:
            subprocess.check_call(
                ["npm", "install", "-g", "@anthropic-ai/claude-code"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            logger.info("Successfully installed Claude Code CLI")

            # Verify installation
            if not ClaudeCodeAgent.check_installation():
                raise RuntimeError("Claude Code CLI installation appeared to succeed but command is still not available")

        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            error_msg = f"Failed to auto-install Claude Code CLI: {e}\n"
            if isinstance(e, FileNotFoundError):
                error_msg += "npm is not installed. Please install Node.js and npm first.\n"
            error_msg += (
                "Please install Claude Code manually using:\n"
                "  npm install -g @anthropic-ai/claude-code\n"
                "Or visit: https://docs.claude.ai/claude-code"
            )
            raise RuntimeError(error_msg)

    ALLOWED_TOOLS = [
        "Bash",
        "Edit",
        "Write",
        "Read",
        "Glob",
        "Grep",
        "LS",
        "WebFetch",
        "NotebookEdit",
        "NotebookRead",
        "TodoRead",
        "TodoWrite",
        "Agent",
        "Skill",
        "SlashCommand",
        "Task",
        "WebSearch",
    ]

    def __init__(
        self,
        logs_dir: Path,
        model_name: str,
        sessions_dir: Optional[Path] = None,
    ):
        """
        Initialize the Claude Code agent.

        Args:
            logs_dir: Directory to store logs and output
            model_name: Model name to use (e.g., "claude-sonnet-4-5")
            sessions_dir: Directory for Claude Code sessions (defaults to logs_dir/sessions)
        """
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        self.model_name = model_name
        self.sessions_dir = Path(sessions_dir) if sessions_dir else self.logs_dir / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Initialized Claude Code agent with model={model_name}")
        logger.info(f"Logs dir: {self.logs_dir}")
        logger.info(f"Sessions dir: {self.sessions_dir}")

    @property
    def output_path(self) -> Path:
        """Path to Claude Code output file."""
        return self.logs_dir / self._OUTPUT_FILENAME

    @property
    def trajectory_path(self) -> Path:
        """Path to trajectory JSON file."""
        return self.logs_dir / "trajectory.json"

    def _get_session_dir(self) -> Path | None:
        """Identify the Claude session directory containing the primary JSONL log"""
        sessions_root = self.sessions_dir
        if not sessions_root.exists():
            return None

        project_root = sessions_root / "projects"
        candidate_files: list[Path] = []
        if project_root.exists():
            candidate_files = list(project_root.glob("**/*.jsonl"))
        if not candidate_files:
            return None

        candidate_dirs = sorted({f.parent for f in candidate_files if f.parent.is_dir()})
        if not candidate_dirs:
            return None

        if len(candidate_dirs) == 1:
            return candidate_dirs[0]

        logger.warning("Multiple Claude Code session directories found; " "could not identify the correct one")
        return None

    def get_usage_metrics(self) -> dict[str, int]:
        """
        Extract usage metrics from Claude Code session files.

        Returns:
            Dictionary with keys: input_tokens, cached_input_tokens, output_tokens
        """
        metrics = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
        }

        session_dir = self._get_session_dir()
        if not session_dir:
            logger.debug("No Claude Code session directory found")
            return metrics

        session_files = list(session_dir.glob("*.jsonl"))
        if not session_files:
            logger.debug(f"No session files found in {session_dir}")
            return metrics

        total_input_tokens = 0
        total_cached_tokens = 0
        total_output_tokens = 0

        for session_file in session_files:
            with open(session_file, "r") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        event = json.loads(stripped)
                        message = event.get("message")
                        if not isinstance(message, dict):
                            continue

                        usage = message.get("usage")
                        if isinstance(usage, dict):
                            total_input_tokens += usage.get("input_tokens", 0)
                            total_cached_tokens += usage.get("cache_read_input_tokens", 0)
                            total_output_tokens += usage.get("output_tokens", 0)
                    except json.JSONDecodeError:
                        continue

        metrics["input_tokens"] = total_input_tokens
        metrics["cached_input_tokens"] = total_cached_tokens
        metrics["output_tokens"] = total_output_tokens

        logger.info(f"Extracted usage metrics: {metrics}")
        return metrics

    def _setup_sessions_structure(self) -> None:
        """Create required Claude Code session directory structure."""
        (self.sessions_dir / "debug").mkdir(parents=True, exist_ok=True)
        (self.sessions_dir / "projects" / "-app").mkdir(parents=True, exist_ok=True)
        (self.sessions_dir / "shell-snapshots").mkdir(parents=True, exist_ok=True)
        (self.sessions_dir / "statsig").mkdir(parents=True, exist_ok=True)
        (self.sessions_dir / "todos").mkdir(parents=True, exist_ok=True)

        logger.info(f"Created session directory structure at {self.sessions_dir}")

    def run(self, instruction: str) -> int:
        """
        Run the Claude Code agent with the given instruction.

        Args:
            instruction: The task instruction to pass to Claude Code

        Returns:
            Return code from Claude Code execution (0 for success)
        """
        # Extract model name (remove provider prefix if present)
        model = self.model_name.split("/")[-1]

        # Default to "sonnet" if model name contains non-Anthropic provider patterns
        invalid_patterns = ["bedrock", "litellm", "azure", "openai", "watsonx", "gemini"]
        if any(pattern in model.lower() for pattern in invalid_patterns):
            logger.warning(
                f"Model '{model}' appears to be for a non-Anthropic provider. "
                f"Defaulting to 'sonnet' for Claude Code."
            )
            model = "sonnet"

        logger.info(f"Running Claude Code with instruction: {instruction}")
        logger.info(f"Using model: {model}")

        # Setup session directory structure
        self._setup_sessions_structure()

        # Build environment variables
        env = os.environ.copy()
        env["CLAUDE_CONFIG_DIR"] = str(self.sessions_dir)
        env["FORCE_AUTO_BACKGROUND_TASKS"] = "1"
        env["ENABLE_BACKGROUND_TASKS"] = "1"

        # Set API key if available
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")

        if not api_key and not oauth_token:
            logger.error("=" * 80)
            logger.error("ERROR: No Anthropic API authentication found")
            logger.error("Please set one of the following environment variables:")
            logger.error("  - ANTHROPIC_API_KEY")
            logger.error("  - CLAUDE_CODE_OAUTH_TOKEN")
            logger.error("=" * 80)
            return 1

        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key
        if oauth_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token

        # Set model name
        env["ANTHROPIC_MODEL"] = model

        # Pass through MAX_THINKING_TOKENS if set
        if "MAX_THINKING_TOKENS" in os.environ:
            env["MAX_THINKING_TOKENS"] = os.environ["MAX_THINKING_TOKENS"]

        # Build Claude Code command
        command = [
            "claude",
            "--verbose",
            "--output-format",
            "stream-json",
            "-p",
            instruction,
            "--allowedTools",
        ] + self.ALLOWED_TOOLS

        logger.info(f"Executing command: {' '.join(command)}")

        try:
            # Run Claude Code and capture output
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

            logger.info(f"Claude Code finished with return code: {process.returncode}")
            return process.returncode

        except Exception as e:
            logger.error(f"Error running Claude Code: {e}")
            raise
