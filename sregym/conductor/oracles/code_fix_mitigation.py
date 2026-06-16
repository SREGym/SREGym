"""Oracle for verifying code-level bug fixes.

This oracle extends MitigationOracle to verify that a code fix:
1. Results in pods recovering to Running state
2. Does not produce the original error patterns in logs
3. Remains stable over a time window
"""

import logging
import subprocess
import time

from sregym.conductor.oracles.mitigation import MitigationOracle

logger = logging.getLogger(__name__)


class CodeFixMitigationOracle(MitigationOracle):
    """Verifies a code fix resolved the issue.

    Checks:
    1. Pods recovered to Running state (inherited from MitigationOracle)
    2. No error patterns in recent logs
    3. Stability over a configurable time window

    Args:
        problem: The problem instance
        error_patterns: List of error strings that should NOT appear in logs after fix
        stability_window: Seconds to wait and re-check stability (default: 120)
    """

    def __init__(self, problem, error_patterns: list[str], stability_window: int = 120):
        super().__init__(problem)
        self.error_patterns = error_patterns
        self.stability_window = stability_window

    def evaluate(self, solution=None, trace=None, duration=None) -> dict:
        """Evaluate whether the code fix resolved the issue.

        Returns:
            dict: {
                "success": bool,
                "stage": str (where evaluation stopped),
                "reason": str (explanation),
                "logs_checked": bool,
                "stability_checked": bool,
            }
        """
        logger.info("== Code Fix Mitigation Evaluation ==")

        # Stage 1: Check pods are running (parent class)
        logger.info("Stage 1: Checking pod health...")
        base_result = super().evaluate()
        if not base_result.get("success"):
            return {
                **base_result,
                "stage": "initial_pod_health",
                "reason": "Pods not healthy after rebuild",
                "logs_checked": False,
                "stability_checked": False,
            }
        logger.info("✓ Pods are healthy")

        # Stage 2: Check logs for error patterns
        logger.info("Stage 2: Checking logs for error patterns...")
        logs = self._get_recent_logs()
        for pattern in self.error_patterns:
            if pattern in logs:
                logger.warning(f"✗ Error pattern found in logs: {pattern}")
                return {
                    "success": False,
                    "stage": "log_check",
                    "reason": f"Error pattern still present in logs: {pattern}",
                    "logs_checked": True,
                    "stability_checked": False,
                }
        logger.info("✓ No error patterns found in logs")

        # Stage 3: Wait for stability window
        logger.info(f"Stage 3: Waiting {self.stability_window}s stability window...")
        time.sleep(self.stability_window)

        # Stage 4: Re-check pods are still running
        logger.info("Stage 4: Re-checking pod health after stability window...")
        recheck_result = super().evaluate()
        if not recheck_result.get("success"):
            return {
                **recheck_result,
                "stage": "stability_recheck",
                "reason": "Pods crashed during stability window - fix may be incomplete",
                "logs_checked": True,
                "stability_checked": True,
            }
        logger.info("✓ Pods still healthy after stability window")

        # Stage 5: Re-check logs after stability window
        logger.info("Stage 5: Re-checking logs after stability window...")
        logs_after = self._get_recent_logs()
        for pattern in self.error_patterns:
            if pattern in logs_after:
                logger.warning(f"✗ Error pattern appeared during stability window: {pattern}")
                return {
                    "success": False,
                    "stage": "stability_log_check",
                    "reason": f"Error pattern appeared during stability window: {pattern}",
                    "logs_checked": True,
                    "stability_checked": True,
                }
        logger.info("✓ No error patterns after stability window")

        logger.info("✓ Code fix verified successfully!")
        return {
            "success": True,
            "stage": "complete",
            "reason": "Pods stable, no error patterns in logs",
            "logs_checked": True,
            "stability_checked": True,
        }

    def _get_recent_logs(self) -> str:
        """Fetch recent logs from Cassandra pods.

        Returns:
            str: Combined logs from all Cassandra pods (last 1000 lines per pod)
        """
        namespace = self.problem.namespace

        # Try to get logs using kubectl
        # K8ssandra uses app.kubernetes.io/name=cassandra label
        cmd = [
            "kubectl",
            "logs",
            "-l",
            "app.kubernetes.io/name=cassandra",
            "-n",
            namespace,
            "--tail=1000",
            "--all-containers",
            "--ignore-errors",
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                return result.stdout
            else:
                logger.warning(f"kubectl logs returned non-zero: {result.stderr}")
                return result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            logger.error("kubectl logs timed out")
            return ""
        except Exception as e:
            logger.error(f"Failed to get logs: {e}")
            return ""
