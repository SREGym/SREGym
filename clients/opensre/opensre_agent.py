"""
OpenSRE agent wrapper for SREGym.

Treats the installed ``opensre`` CLI (on PATH, from the official installer /
package) as a black-box diagnosis engine. It builds a generic alert from the
SREGym problem, runs ``opensre investigate``, and parses the structured JSON
report back into a natural-language diagnosis SREGym's judge can grade.

This module deliberately does NOT import from the OpenSRE source tree. OpenSRE
is only ever invoked as a subprocess, the same way ``ClaudeCodeAgent`` shells
out to the ``claude`` binary.
"""

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger("all.opensre.agent")


class OpenSREAgent:
    """Wraps the installed ``opensre`` CLI as a diagnosis planner."""

    _ALERT_FILENAME = "opensre-alert.json"
    _OUTPUT_FILENAME = "opensre-investigation.json"

    # Keys we try, in order, when pulling fields out of OpenSRE's report JSON.
    # OpenSRE is a black box and its payload schema may evolve, so we look these
    # up defensively rather than assuming one exact shape.
    _ROOT_CAUSE_KEYS = ("root_cause", "root_cause_summary", "diagnosis", "conclusion")
    _REMEDIATION_KEYS = ("remediation_steps", "remediation", "next_steps", "suggested_actions")
    _REPORT_KEYS = ("report", "final_report", "summary")

    @staticmethod
    def check_installation() -> bool:
        """True if the ``opensre`` CLI is available on PATH."""
        return shutil.which("opensre") is not None

    @staticmethod
    def ensure_installed() -> None:
        """Raise a helpful error if the ``opensre`` CLI is not installed.

        We never auto-install OpenSRE — it is an external package the operator
        installs via the official channel.
        """
        if OpenSREAgent.check_installation():
            logger.info("OpenSRE CLI found on PATH")
            return
        raise RuntimeError(
            "The 'opensre' CLI is not installed or not on PATH.\n"
            "Install it via the official installer:\n"
            "  curl -fsSL https://install.opensre.com | bash\n"
            "then ensure `opensre` is on your PATH (e.g. ~/.local/bin)."
        )

    def __init__(self, logs_dir: Path, model_name: str | None = None, timeout: int = 1800):
        """
        Args:
            logs_dir: Directory for the alert file, raw report, and logs.
            model_name: Informational only. OpenSRE selects its own LLM via its
                own configuration (LLM_PROVIDER + provider key); we do not
                override it here to avoid reaching into OpenSRE internals.
            timeout: Max seconds to allow a single investigation to run.
        """
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.model_name = model_name
        self.timeout = timeout
        self._last_result: dict[str, Any] = {}
        logger.info(f"Initialized OpenSRE agent (logs_dir={self.logs_dir})")

    @property
    def alert_path(self) -> Path:
        return self.logs_dir / self._ALERT_FILENAME

    @property
    def output_path(self) -> Path:
        return self.logs_dir / self._OUTPUT_FILENAME

    def investigate(self, incident: str, title: str = "SREGym incident") -> dict[str, Any]:
        """Run ``opensre investigate`` against a generic alert built from ``incident``.

        Returns the parsed report dict (empty dict on failure).
        """
        alert = {
            "alert_source": "generic",
            "title": title,
            "message": incident,
        }
        self.alert_path.write_text(json.dumps(alert, indent=2), encoding="utf-8")

        command = [
            "opensre",
            "investigate",
            "-i",
            str(self.alert_path),
            "-o",
            str(self.output_path),
        ]
        logger.info(f"Running OpenSRE: {' '.join(command)}")

        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            logger.error(f"OpenSRE investigation timed out after {self.timeout}s")
            return {}
        except FileNotFoundError:
            logger.error("OpenSRE CLI disappeared from PATH during run")
            return {}

        if proc.returncode != 0:
            logger.error(f"OpenSRE exited {proc.returncode}: {proc.stderr[-2000:] if proc.stderr else ''}")
            # Still try to read any partial output written before failure.

        result = self._load_output()
        self._last_result = result
        return result

    def _load_output(self) -> dict[str, Any]:
        if not self.output_path.exists():
            logger.warning(f"OpenSRE produced no output file at {self.output_path}")
            return {}
        try:
            data = json.loads(self.output_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error(f"Could not parse OpenSRE output: {exc}")
            return {}
        return data if isinstance(data, dict) else {"raw": data}

    @staticmethod
    def _first_present(result: dict[str, Any], keys: tuple[str, ...]) -> Any:
        for key in keys:
            if key in result and result[key]:
                return result[key]
        return None

    def diagnosis_text(self, result: dict[str, Any] | None = None) -> str:
        """Flatten OpenSRE's report into a natural-language diagnosis for /submit.

        SREGym's diagnosis oracle is an LLM judge over free text, so we hand it
        OpenSRE's root cause plus (if present) its remediation steps.
        """
        result = result if result is not None else self._last_result
        if not result:
            return "OpenSRE produced no diagnosis."

        root_cause = self._first_present(result, self._ROOT_CAUSE_KEYS)
        remediation = self._first_present(result, self._REMEDIATION_KEYS)
        report = self._first_present(result, self._REPORT_KEYS)

        parts: list[str] = []
        if root_cause:
            parts.append(f"Root cause: {self._stringify(root_cause)}")
        if remediation:
            parts.append(f"Suggested remediation: {self._stringify(remediation)}")
        if not parts and report:
            # Fall back to the full report if we could not find structured fields.
            parts.append(self._stringify(report))
        if not parts:
            parts.append(self._stringify(result))
        return "\n\n".join(parts)

    def remediation_plan(self, result: dict[str, Any] | None = None) -> str:
        """The root cause + remediation steps, formatted for the executor prompt."""
        result = result if result is not None else self._last_result
        root_cause = self._first_present(result, self._ROOT_CAUSE_KEYS)
        remediation = self._first_present(result, self._REMEDIATION_KEYS)
        lines: list[str] = []
        if root_cause:
            lines.append(f"OpenSRE identified this root cause:\n{self._stringify(root_cause)}")
        if remediation:
            lines.append(f"OpenSRE's recommended remediation steps:\n{self._stringify(remediation)}")
        if not lines:
            lines.append("OpenSRE did not return a structured plan; investigate and fix directly.")
        return "\n\n".join(lines)

    @staticmethod
    def _stringify(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            return "\n".join(f"- {OpenSREAgent._stringify(v)}" for v in value)
        if isinstance(value, dict):
            return json.dumps(value, indent=2)
        return str(value)

    def get_usage_metrics(self) -> dict[str, int]:
        """Best-effort token/cost extraction from OpenSRE's report (zeros if absent)."""
        metrics = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
        usage = self._last_result.get("usage") or self._last_result.get("token_usage")
        if isinstance(usage, dict):
            metrics["input_tokens"] = int(usage.get("input_tokens", 0) or 0)
            metrics["cached_input_tokens"] = int(usage.get("cached_input_tokens", 0) or 0)
            metrics["output_tokens"] = int(usage.get("output_tokens", 0) or 0)
        return metrics
