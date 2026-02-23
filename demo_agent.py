"""
Demo Agent that executes pre-scripted kubectl commands from a file.

This agent reads commands line-by-line and executes them sequentially.
Works with any agent type and model.
"""

import argparse
import logging
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("demo_agent")
logger.propagate = True
logger.setLevel(logging.DEBUG)


class DemoAgent:
    """A demo agent that executes kubectl commands from kubectl_cmds.txt sequentially."""

    def __init__(
        self,
        kubectl_cmds_file: str = "kubectl_cmds.txt",
        agent: str | None = None,
        model: str | None = None,
    ):
        """Initialize the demo agent.

        Args:
            kubectl_cmds_file: Path to file containing kubectl commands (one per line)
            agent: Agent type (for logging/tracking purposes)
            model: Model being used (for logging/tracking purposes)
        """
        self.kubectl_cmds_file = Path(kubectl_cmds_file)
        self.commands: list[str] = []
        self.results: list[dict] = []
        self.step = 0
        self.agent = agent
        self.model = model

    def load_commands(self) -> bool:
        """Load kubectl commands from file.

        Ignores empty lines and lines starting with '#'.

        Returns:
            True if commands loaded successfully, False otherwise
        """
        if not self.kubectl_cmds_file.exists():
            logger.error(f"Command file not found: {self.kubectl_cmds_file}")
            return False

        try:
            with open(self.kubectl_cmds_file) as f:
                for line in f:
                    line = line.strip()
                    # Skip empty lines and comments
                    if line and not line.startswith("#"):
                        self.commands.append(line)

            logger.info(
                f"Loaded {len(self.commands)} commands from {self.kubectl_cmds_file}"
            )
            return True
        except Exception as e:
            logger.error(f"Failed to load commands: {e}")
            return False

    def execute_command(self, cmd: str, timeout: int = 30) -> dict:
        """Execute a single kubectl command.

        Args:
            cmd: The kubectl command to execute
            timeout: Timeout in seconds

        Returns:
            Dict with 'success', 'stdout', 'stderr', and 'return_code'
        """
        logger.debug(f"[Step {self.step}] Executing: {cmd}")
        print(f"\n{'=' * 70}")
        print(f"Step {self.step}: {cmd}")
        print("=" * 70)

        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            output = {
                "step": self.step,
                "command": cmd,
                "success": result.returncode == 0,
                "return_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "timestamp": time.time(),
            }

            # Print output
            if result.stdout:
                print(result.stdout)
            if result.stderr:
                print(f"[STDERR] {result.stderr}")

            if result.returncode == 0:
                logger.info(f"[Step {self.step}] Command succeeded")
            else:
                logger.warning(
                    f"[Step {self.step}] Command failed with return code {result.returncode}"
                )

            return output

        except subprocess.TimeoutExpired:
            logger.error(f"[Step {self.step}] Command timed out after {timeout}s")
            return {
                "step": self.step,
                "command": cmd,
                "success": False,
                "return_code": -1,
                "stdout": "",
                "stderr": f"Timeout after {timeout}s",
                "timestamp": time.time(),
            }
        except Exception as e:
            logger.error(f"[Step {self.step}] Error executing command: {e}")
            return {
                "step": self.step,
                "command": cmd,
                "success": False,
                "return_code": -1,
                "stdout": "",
                "stderr": str(e),
                "timestamp": time.time(),
            }

    def run(
        self, pause_between_steps: float = 1.0, continue_on_error: bool = False
    ) -> list[dict]:
        """Execute all commands sequentially.

        Args:
            pause_between_steps: Pause in seconds between commands for readability
            continue_on_error: If False, stop on first error; if True, continue anyway

        Returns:
            List of execution results
        """
        if not self.commands:
            if not self.load_commands():
                return []

        agent_info = (
            f"Agent: {self.agent}, Model: {self.model}"
            if self.agent and self.model
            else ""
        )
        logger.info(
            f"Starting demo agent with {len(self.commands)} commands. {agent_info}"
        )
        print(f"\n{'=' * 70}")
        print(f"DEMO AGENT - Executing {len(self.commands)} commands")
        if agent_info:
            print(agent_info)
        print("=" * 70)

        for cmd in self.commands:
            result = self.execute_command(cmd)
            self.results.append(result)
            self.step += 1

            # Check if we should continue on error
            if not result["success"] and not continue_on_error:
                logger.error("Stopping due to command failure")
                break

            # Pause for readability
            if pause_between_steps > 0:
                time.sleep(pause_between_steps)

        logger.info(f"Demo agent completed {self.step} steps")
        self._print_summary()
        return self.results

    def _print_summary(self) -> None:
        """Print summary of execution results."""
        successful = sum(1 for r in self.results if r["success"])
        failed = len(self.results) - successful

        print(f"\n{'=' * 70}")
        total_commands = len(self.results)
        print(
            f"SUMMARY: {successful} successful, {failed} failed out of {total_commands} commands"
        )
        print("=" * 70)

        for result in self.results:
            status = "✓" if result["success"] else "✗"
            print(f"{status} Step {result['step']}: {result['command'][:60]}")


def main() -> None:
    """Run demo agent with kubectl commands."""
    parser = argparse.ArgumentParser(description="Run demo agent with kubectl commands")
    parser.add_argument(
        "--agent",
        default="stratus",
        help="Agent type (e.g., stratus, claudecode, geminicli)",
    )
    parser.add_argument(
        "--model",
        default="gpt-4o",
        help="Model to use (e.g., gpt-4o, claude-3-5-sonnet, gemini-2.0)",
    )
    parser.add_argument(
        "--file", default="kubectl_cmds.txt", help="Path to kubectl commands file"
    )
    parser.add_argument(
        "--pause", type=float, default=1.0, help="Pause between steps (seconds)"
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue even if a command fails",
    )

    args = parser.parse_args()

    agent = DemoAgent(args.file, agent=args.agent, model=args.model)
    agent.run(pause_between_steps=args.pause, continue_on_error=args.continue_on_error)


if __name__ == "__main__":
    main()
