import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

USAGE_LOG_PATH_ENV = "LLM_USAGE_LOG_PATH"


def record_usage(completion: Any, *, model: str, usage_type: str, duration_seconds: float) -> None:
    """Persist one completed LLM request when usage logging is enabled."""
    log_path = os.environ.get(USAGE_LOG_PATH_ENV)
    if not log_path:
        return

    try:
        usage = getattr(completion, "usage_metadata", None)
        if not usage:
            logger.warning("LLM response did not include usage metadata; usage was not recorded")
            return

        response_metadata = getattr(completion, "response_metadata", None) or {}
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "model": response_metadata.get("model_name") or model,
            "usage_type": usage_type,
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "input_token_details": usage.get("input_token_details", {}),
            "output_token_details": usage.get("output_token_details", {}),
            "duration_seconds": duration_seconds,
        }
        payload = (json.dumps(record, default=str) + "\n").encode()

        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception as error:
        # Usage reporting must not stop the benchmark agent.
        logger.warning("Could not persist LLM usage to %s: %s", log_path, error)


def summarize_usage(log_path: Path, *, usage_type: str | None = None) -> dict[str, int | float]:
    """Return totals from complete records, ignoring a partial final line."""
    totals: dict[str, int | float] = {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "duration_seconds": 0.0,
    }
    if not log_path.exists():
        return totals

    with log_path.open(encoding="utf-8") as log_file:
        for line in log_file:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if usage_type is not None and record.get("usage_type") != usage_type:
                continue
            totals["requests"] += 1
            totals["input_tokens"] += int(record.get("input_tokens", 0))
            totals["output_tokens"] += int(record.get("output_tokens", 0))
            totals["total_tokens"] += int(record.get("total_tokens", 0))
            totals["duration_seconds"] += float(record.get("duration_seconds", 0.0))
    return totals
