"""
TierZero agent driver for SREGym.

Orchestrates the two-phase benchmark flow (diagnosis + mitigation) by calling
TierZero's REST API and submitting results to the SREGym conductor.

TierZero runs as an external service -- the agent investigates via MCP tools
(kubectl, prometheus, jaeger, loki) that are pre-configured on the TierZero org
and tunneled to the SREGym MCP server via ngrok.
"""

import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

# Add SREGym root to path
sregym_root = Path(__file__).resolve().parents[2]
if str(sregym_root) not in sys.path:
    sys.path.insert(0, str(sregym_root))

from logger import init_logger  # noqa: E402

init_logger()

logger = logging.getLogger("all.tierzero.driver")

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------

API_HOSTNAME = os.getenv("API_HOSTNAME", "localhost")
API_PORT = os.getenv("API_PORT", "8000")
CONDUCTOR_URL = f"http://{API_HOSTNAME}:{API_PORT}"

TIERZERO_API_URL = os.environ.get("TIERZERO_API_URL", "https://api.tierzero.ai")
TIERZERO_ORG_API_KEY = os.environ.get("TIERZERO_ORG_API_KEY", "")

AGENT_LOGS_DIR = os.environ.get("AGENT_LOGS_DIR", "./logs/tierzero")

# Timeouts
INTERACTION_POLL_INTERVAL = int(os.environ.get("TIERZERO_POLL_INTERVAL", "10"))
INTERACTION_TIMEOUT = int(os.environ.get("TIERZERO_INTERACTION_TIMEOUT", "1500"))


# ---------------------------------------------------------------------------
# TierZero API helpers
# ---------------------------------------------------------------------------


def tierzero_headers() -> dict:
    return {
        "X-TierZero-Org-Api-Key": TIERZERO_ORG_API_KEY,
        "Content-Type": "application/json",
    }


def create_interaction(question: str, context: list[dict] | None = None) -> str:
    """Create an async TierZero interaction. Returns interaction_id."""
    payload = {
        "question": question,
        "use_tools": True,
        "scheduled_runtime": int(time.time()) + 5,
    }
    if context:
        payload["context"] = context

    url = f"{TIERZERO_API_URL}/api/v1/interactions"
    logger.info(f"Creating TierZero interaction: {url}")
    logger.info(f"Prompt ({len(question)} chars): {question[:200]}...")

    resp = requests.post(url, json=payload, headers=tierzero_headers(), timeout=30)
    if not resp.ok:
        logger.error(f"TierZero API error: {resp.status_code} {resp.text}")
    resp.raise_for_status()
    data = resp.json()

    interaction_id = data["interaction_id"]
    logger.info(f"Interaction created: {interaction_id} (status={data.get('status')})")
    return interaction_id


def poll_interaction(interaction_id: str) -> dict:
    """Poll until interaction is COMPLETED or FAILED. Returns response dict."""
    url = f"{TIERZERO_API_URL}/api/v1/interactions/{interaction_id}"
    start = time.time()
    was_in_progress = False
    consecutive_errors = 0

    while time.time() - start < INTERACTION_TIMEOUT:
        try:
            resp = requests.get(url, headers=tierzero_headers(), timeout=30)
            data = resp.json() if resp.ok else {}
            status = data.get("status")

            if resp.status_code == 404 and was_in_progress:
                # Interaction completed but conversation data not found — treat as completed
                detail = data.get("detail", resp.text)
                logger.warning(f"Interaction {interaction_id} returned 404 after IN_PROGRESS: {detail}")
                return {"status": "COMPLETED", "content": "", "interaction_id": interaction_id}

            if not resp.ok:
                consecutive_errors += 1
                elapsed = int(time.time() - start)
                logger.warning(f"Poll error {resp.status_code} (attempt {consecutive_errors}), elapsed={elapsed}s")
                if consecutive_errors > 10:
                    logger.error(f"Too many consecutive errors polling {interaction_id}")
                    return {"status": "FAILED", "content": ""}
                time.sleep(INTERACTION_POLL_INTERVAL)
                continue

            consecutive_errors = 0

            if status == "COMPLETED":
                logger.info(f"Interaction {interaction_id} completed")
                return data
            if status == "FAILED":
                logger.error(f"Interaction {interaction_id} failed")
                return data

            if status == "IN_PROGRESS":
                was_in_progress = True

            elapsed = int(time.time() - start)
            logger.info(f"Interaction {interaction_id}: status={status}, elapsed={elapsed}s")
        except requests.RequestException as e:
            consecutive_errors += 1
            logger.warning(f"Poll error (will retry): {e}")

        time.sleep(INTERACTION_POLL_INTERVAL)

    raise TimeoutError(f"Interaction {interaction_id} timed out after {INTERACTION_TIMEOUT}s")


def extract_content(result: dict) -> str:
    """Extract text content from a completed interaction result."""
    return result.get("content", "")


# ---------------------------------------------------------------------------
# SREGym conductor helpers
# ---------------------------------------------------------------------------


def get_app_info(max_retries: int = 6, backoff: int = 5) -> dict:
    """Fetch application info from conductor."""
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(f"{CONDUCTOR_URL}/get_app", timeout=10)
            resp.raise_for_status()
            info = resp.json()
            logger.info(f"App info: {info}")
            return info
        except Exception as e:
            if attempt < max_retries:
                logger.warning(f"get_app attempt {attempt}/{max_retries} failed: {e}")
                time.sleep(backoff)
            else:
                raise


def get_problem_id() -> str:
    """Fetch current problem ID from conductor."""
    resp = requests.get(f"{CONDUCTOR_URL}/get_problem", timeout=10)
    resp.raise_for_status()
    problem_id = resp.json().get("problem_id", "unknown")
    logger.info(f"Problem ID: {problem_id}")
    return problem_id


def wait_for_stage(target_stages: set[str], timeout: int = 300) -> str:
    """Poll conductor until stage is in target_stages."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(f"{CONDUCTOR_URL}/status", timeout=10)
            resp.raise_for_status()
            stage = resp.json().get("stage", "")
            if stage in target_stages:
                logger.info(f"Conductor reached stage: {stage}")
                return stage
        except Exception as e:
            logger.debug(f"Status poll error: {e}")
        time.sleep(2)

    raise TimeoutError(f"Conductor did not reach {target_stages} within {timeout}s")


def submit_to_conductor(solution: str) -> None:
    """POST /submit to conductor."""
    logger.info(f"Submitting to conductor ({len(solution)} chars)")
    resp = requests.post(
        f"{CONDUCTOR_URL}/submit",
        json={"solution": solution},
        timeout=30,
    )
    if not resp.ok:
        logger.error(f"Submit failed: {resp.status_code} {resp.text}")
    resp.raise_for_status()
    result = resp.json()
    logger.info(f"Submit response: {result}")


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

DIAGNOSIS_PROMPT = """You are investigating a Kubernetes application failure.

Application: {app_name}
Namespace: {namespace}
Description: {descriptions}

Your task is to diagnose the root cause of the failure in this application.

Use the available MCP tools (kubectl, prometheus, jaeger, loki) to investigate:
1. Check pod status, events, and describe problematic resources via kubectl
2. Query Prometheus for error rates, latency, and resource usage metrics
3. Examine Jaeger traces for failing request paths and dependency issues
4. Search Loki logs for error patterns and crash messages

Provide a clear, concise diagnosis identifying:
- What component is failing and how
- The root cause of the failure
- Supporting evidence from your investigation

Be specific and technical. State the root cause clearly."""

MITIGATION_PROMPT = """Your previous diagnosis is available in the conversation context.

Application: {app_name}
Namespace: {namespace}

Your task is to FIX the issue using kubectl commands via the MCP kubectl tool.

Instructions:
1. Based on your diagnosis, determine the appropriate fix
2. Apply the fix using kubectl commands (patch, edit, scale, rollout, apply, etc.)
3. Verify the fix by checking that pods are Running and containers are Ready
4. Confirm services are responding correctly

After applying the fix, provide a summary of what you changed and verification that it is working."""


# ---------------------------------------------------------------------------
# Main driver loop
# ---------------------------------------------------------------------------


def main():
    # Setup logging
    logs_dir = Path(AGENT_LOGS_DIR)
    logs_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(logs_dir / "driver.log")
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    logging.getLogger().addHandler(file_handler)

    logger.info("=" * 60)
    logger.info("TierZero driver starting")
    logger.info(f"Conductor: {CONDUCTOR_URL}")
    logger.info(f"TierZero API: {TIERZERO_API_URL}")
    logger.info("=" * 60)

    if not TIERZERO_ORG_API_KEY:
        logger.error("TIERZERO_ORG_API_KEY is not set")
        sys.exit(1)

    # ---- Wait for conductor to be ready for diagnosis ----
    try:
        stage = wait_for_stage({"diagnosis"}, timeout=300)
    except TimeoutError:
        logger.error("Timed out waiting for conductor to reach diagnosis stage")
        sys.exit(1)

    # ---- Get problem info ----
    app_info = get_app_info()
    problem_id = get_problem_id()

    params = {
        "app_name": app_info.get("app_name", "unknown"),
        "namespace": app_info.get("namespace", "default"),
        "descriptions": app_info.get("descriptions", ""),
    }

    # ================================================================
    # PHASE 1: DIAGNOSIS
    # ================================================================
    logger.info("=" * 40 + " DIAGNOSIS " + "=" * 40)

    diagnosis_prompt = DIAGNOSIS_PROMPT.format(**params)
    diagnosis_interaction_id = None
    diagnosis_text = ""

    try:
        diagnosis_interaction_id = create_interaction(diagnosis_prompt)
        result = poll_interaction(diagnosis_interaction_id)

        if result.get("status") == "COMPLETED":
            diagnosis_text = extract_content(result)
            logger.info(f"Diagnosis result ({len(diagnosis_text)} chars): {diagnosis_text[:300]}...")
        else:
            logger.error(f"Diagnosis interaction status: {result.get('status')}")
            diagnosis_text = "Unable to complete diagnosis"
    except Exception as e:
        logger.error(f"Diagnosis phase failed: {e}")
        diagnosis_text = "Unable to complete diagnosis"

    # Submit diagnosis to conductor
    submit_to_conductor(diagnosis_text)

    # Save diagnosis result
    _save_stage_result(logs_dir, "diagnosis", diagnosis_interaction_id, diagnosis_text)

    # ---- Wait for conductor to transition to mitigation ----
    try:
        stage = wait_for_stage({"mitigation", "done"}, timeout=300)
    except TimeoutError:
        logger.error("Timed out waiting for mitigation stage")
        sys.exit(1)

    if stage == "done":
        logger.info("Conductor went straight to done after diagnosis")
        _finish(logs_dir, problem_id)
        return

    # ================================================================
    # PHASE 2: MITIGATION
    # ================================================================
    logger.info("=" * 40 + " MITIGATION " + "=" * 39)

    mitigation_prompt = MITIGATION_PROMPT.format(**params)

    # Chain context from diagnosis phase so the agent has full conversation history
    context = [{"interaction_id": diagnosis_interaction_id}] if diagnosis_interaction_id else None

    mitigation_interaction_id = None
    mitigation_text = ""

    try:
        mitigation_interaction_id = create_interaction(mitigation_prompt, context=context)
        result = poll_interaction(mitigation_interaction_id)

        if result.get("status") == "COMPLETED":
            mitigation_text = extract_content(result)
            logger.info(f"Mitigation result ({len(mitigation_text)} chars): {mitigation_text[:300]}...")
        else:
            logger.error(f"Mitigation interaction status: {result.get('status')}")
    except Exception as e:
        logger.error(f"Mitigation phase failed: {e}")

    # Submit empty string for mitigation (fix has been applied via kubectl)
    submit_to_conductor("")

    # Save mitigation result
    _save_stage_result(logs_dir, "mitigation", mitigation_interaction_id, mitigation_text)

    # ---- Handle resolution stage if present, then wait for done ----
    try:
        stage = wait_for_stage({"resolution", "done", "tearing_down"}, timeout=300)
        if stage == "resolution":
            logger.info("Resolution stage reached, submitting empty string")
            submit_to_conductor("")
            wait_for_stage({"done", "tearing_down"}, timeout=300)
    except TimeoutError:
        logger.warning("Timed out waiting for done stage")

    _finish(logs_dir, problem_id)


def _save_stage_result(logs_dir: Path, stage: str, interaction_id: str | None, content: str) -> None:
    result_file = logs_dir / f"{stage}_result.json"
    with open(result_file, "w") as f:
        json.dump(
            {
                "stage": stage,
                "interaction_id": interaction_id,
                "content_length": len(content),
                "timestamp": datetime.now(UTC).isoformat(),
            },
            f,
            indent=2,
        )
    logger.info(f"Saved {stage} result to {result_file}")


def _finish(logs_dir: Path, problem_id: str) -> None:
    summary = {
        "problem_id": problem_id,
        "driver": "tierzero",
        "timestamp": datetime.now(UTC).isoformat(),
    }
    with open(logs_dir / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    logger.info("TierZero driver finished")


if __name__ == "__main__":
    main()
