"""Resolve AI agent driver for SREGym.

Sets up the Resolve satellite and ktunnel, fires a webhook alert to
trigger Resolve AI, then waits for it to complete via MCP tools.
"""

import json
import logging
import os
import shutil
import signal
import sys
import time
from datetime import UTC, datetime

import requests

from clients.resolve.setup import ResolveSetup

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

API_HOSTNAME = os.getenv("API_HOSTNAME", "localhost")
API_PORT = os.getenv("API_PORT", "8000")
CONDUCTOR_URL = f"http://{API_HOSTNAME}:{API_PORT}"

RESOLVE_WEBHOOK_URL = os.environ.get("RESOLVE_WEBHOOK_URL")
RESOLVE_WEBHOOK_TOKEN = os.environ.get("RESOLVE_WEBHOOK_TOKEN")


def get_app_info(max_retries: int = 6, backoff: int = 5) -> dict:
    """Fetch current application info from the conductor API."""
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(f"{CONDUCTOR_URL}/get_app", timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            if attempt < max_retries:
                logger.warning(f"Failed to reach conductor (attempt {attempt}/{max_retries}): {e}")
                time.sleep(backoff)
            else:
                logger.error(f"Failed to get app info from conductor after {max_retries} attempts: {e}")
                sys.exit(1)


def fire_alert(app_info: dict):
    """Send a webhook alert to Resolve AI."""
    if not RESOLVE_WEBHOOK_URL or not RESOLVE_WEBHOOK_TOKEN:
        logger.error(
            "RESOLVE_WEBHOOK_URL and RESOLVE_WEBHOOK_TOKEN must be set. "
            "Example: export RESOLVE_WEBHOOK_URL='https://api.app0.resolve.ai/webhooks/.../integrations/alertWebhook/...'"
        )
        sys.exit(1)

    namespace = app_info.get("namespace", "unknown")
    app_name = app_info.get("app_name", "unknown")
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    payload = {
        "id": f"sregym-{namespace}-{timestamp}",
        "timestamp": timestamp,
        "action": "fire",
        "name": f"Application unhealthy in {namespace} namespace",
        "summary": f"Services in {namespace} namespace are experiencing failures",
        "description": (
            f"The {app_name} application in the {namespace} namespace is unhealthy. "
            "Please investigate the root cause and remediate the issue."
        ),
        "labels": {
            "namespace": namespace,
            "severity": "critical",
            "source": "sregym",
        },
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {RESOLVE_WEBHOOK_TOKEN}",
    }

    logger.info(f"Firing alert to Resolve AI for namespace={namespace}")
    logger.info(f"Payload: {json.dumps(payload, indent=2)}")

    try:
        resp = requests.post(RESOLVE_WEBHOOK_URL, json=payload, headers=headers, timeout=30)
        logger.info(f"Resolve webhook response: {resp.status_code} {resp.text}")
        if not resp.ok:
            logger.error(f"Webhook returned non-200 status: {resp.status_code}")
            sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to fire alert to Resolve: {e}")
        sys.exit(1)


resolve_setup = ResolveSetup()


def _shutdown_handler(signum, frame):
    """Clean up Resolve infrastructure on termination."""
    logger.info("Received shutdown signal, cleaning up Resolve infrastructure...")
    resolve_setup.stop()
    sys.exit(0)


def main():
    logger.info("Resolve AI agent driver starting")

    # The agent launcher sets KUBECONFIG to a filtering proxy, but the
    # Resolve driver manages cluster infrastructure (ktunnel, helm, kubectl
    # patches) that must talk to the real K8s API server.
    os.environ.pop("KUBECONFIG", None)

    # Check that ktunnel is installed
    if not shutil.which("ktunnel"):
        logger.error("ktunnel is not installed. Install it from https://github.com/omrikiei/ktunnel")
        sys.exit(1)

    # Register cleanup handler so ktunnel + satellite are torn down
    # when main.py terminates this process
    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    # Get app info from conductor BEFORE starting ktunnel, because ktunnel
    # binds to local port 8000 and intercepts connections to the conductor.
    app_info = get_app_info()
    logger.info(f"App info: {app_info}")

    # Set up ktunnel and install Resolve satellite
    resolve_setup.start()

    # Fire the alert
    fire_alert(app_info)

    logger.info("Alert fired. Waiting for Resolve AI to complete investigation and submit results...")

    # Wait indefinitely — the main.py driver loop polls conductor.submission_stage
    # and will terminate this process when grading is done.
    signal.pause()


if __name__ == "__main__":
    main()
