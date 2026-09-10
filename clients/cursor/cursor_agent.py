"""
Cursor CLI agent implementation for SREGym.

Wraps Cursor's headless `agent` CLI (https://cursor.com/docs/cli), used as a
low-usage fallback when the primary Z.ai-backed Codex path is out of quota.
Authentication is via the CURSOR_API_KEY environment variable; model choice
is intentionally loose (any model the CLI exposes, including "auto") since
this path only needs to gauge whether a problem is saturated, not compare
models consistently.
"""

import json
import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from clients.harness.token_usage import usage_metrics

logger = logging.getLogger("all.cursor.agent")

_INSTALL_URL = "https://cursor.com/install"


class CursorAgent:
    """
    The Cursor agent uses Cursor's `agent` CLI in non-interactive mode.
    """

    _OUTPUT_FILENAME = "cursor.json"

    @staticmethod
    def check_installation() -> bool:
        """Check if the Cursor CLI (`agent`) is installed."""
        return shutil.which("agent") is not None

    @staticmethod
    def ensure_installed(auto_install: bool = True) -> None:
        """
        Ensure the Cursor CLI is installed, optionally attempting installation.

        Args:
            auto_install: If True, attempt to install the CLI if not found

        Raises:
            RuntimeError: If the CLI is not installed and auto_install fails
        """
        if CursorAgent.check_installation():
            logger.info("Cursor CLI is already installed")
            return

        logger.warning("Cursor CLI not found in PATH")

        if not auto_install:
            raise RuntimeError(
                "Cursor CLI is not installed. Please install it using:\n"
                f"  curl {_INSTALL_URL} -fsS | bash\n"
                "Or visit: https://cursor.com/docs/cli/overview"
            )

        logger.info("Attempting to install Cursor CLI...")
        try:
            subprocess.run(
                f"curl {_INSTALL_URL} -fsS | bash",
                shell=True,
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
            home_bin = Path(os.environ.get("HOME", "/root")) / ".local" / "bin" / "agent"
            if not CursorAgent.check_installation() and home_bin.exists():
                # The installer writes to ~/.cursor/bin, which is only on PATH
                # in login shells. Link it into a directory already on PATH so
                # later, unrelated subprocess invocations can find it too.
                target = Path("/usr/local/bin/agent")
                if not target.exists():
                    target.symlink_to(home_bin)

            if not CursorAgent.check_installation():
                raise RuntimeError("Cursor CLI installation appeared to succeed but 'agent' is still not available")

            logger.info("Successfully installed Cursor CLI")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            raise RuntimeError(
                f"Failed to auto-install Cursor CLI: {e}\n"
                f"Please install it manually using:\n"
                f"  curl {_INSTALL_URL} -fsS | bash\n"
                "Or visit: https://cursor.com/docs/cli/overview"
            ) from None

    def __init__(self, logs_dir: Path, model_name: str):
        """
        Initialize the Cursor agent.

        Args:
            logs_dir: Directory to store logs and output
            model_name: Model id to pass to `agent --model` (e.g. "auto",
                "gpt-5", "sonnet-4.5", "grok-code")
        """
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.model_name = model_name

        if not os.environ.get("CURSOR_API_KEY"):
            logger.warning("CURSOR_API_KEY is not set; the Cursor CLI will not be authenticated")

        logger.info(f"Initialized Cursor agent with model={model_name}")
        logger.info(f"Logs dir: {self.logs_dir}")

    @property
    def output_path(self) -> Path:
        """Path to the raw `agent` CLI JSON output."""
        return self.logs_dir / self._OUTPUT_FILENAME

    def get_usage_metrics(self) -> dict[str, int | None]:
        """
        Cursor's `--output-format json` result does not report token usage,
        so this returns unknown counts. Kept for interface parity with the
        other agent clients, whose usage metrics feed the same report.
        """
        return usage_metrics()

    def generate_trajectory(self, problem_id: str) -> Path | None:
        """Cursor CLI trajectories are not wired into the visualizer yet."""
        del problem_id
        return None

    def _build_command(self, instruction: str) -> list[str]:
        command = [
            "agent",
            "--print",
            instruction,
            "--model",
            self.model_name,
            "--output-format",
            "json",
            "--trust",
        ]
        return command

    def run(self, instruction: str) -> int:
        """
        Run the Cursor agent with the given instruction.

        Args:
            instruction: The task instruction to pass to the agent

        Returns:
            Return code from the `agent` CLI (0 for success)
        """
        logger.info(f"Running Cursor agent with model: {self.model_name}")

        command = self._build_command(instruction)
        logger.info(f"Executing command: {' '.join(command[:4])} ...")

        try:
            with open(self.output_path, "w") as out_file:
                process = subprocess.run(
                    command,
                    stdout=out_file,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                )

            logger.info(f"Cursor agent finished with return code: {process.returncode}")

            if process.returncode != 0:
                # On failure the CLI writes a plain error to stderr/stdout
                # instead of the JSON result object; surface it for the log.
                try:
                    tail = self.output_path.read_text()[-4000:]
                    logger.error(f"Cursor agent output:\n{tail}")
                except OSError:
                    pass

            return process.returncode

        except Exception as e:
            logger.error(f"Error running Cursor agent: {e}")
            raise

    def result_text(self) -> str | None:
        """Best-effort extraction of the final assistant text, if present."""
        if not self.output_path.exists():
            return None
        try:
            data = json.loads(self.output_path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        if isinstance(data, dict):
            return data.get("result")
        return None


def save_results(
    logs_dir: Path,
    problem_id: str,
    return_code: int,
    usage_metrics: dict,
) -> None:
    """Save run results to a JSON file, matching the other agent drivers."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_file = Path(logs_dir) / f"cursor_results_{problem_id}_{timestamp}.json"

    results = {
        "problem_id": problem_id,
        "timestamp": timestamp,
        "return_code": return_code,
        "success": return_code == 0,
        "usage_metrics": usage_metrics,
    }

    with open(results_file, "w") as f:
        json.dump(results, f, indent=2)

    logger.info(f"Saved results to {results_file}")
