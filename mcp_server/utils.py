import logging
import os
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

mcp_server_port = os.getenv("MCP_SERVER_PORT", "8001")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("all.mcp.utils")

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", 120))
RETRY_TOTAL = int(os.getenv("RETRY_TOTAL", 3))
RETRY_BACKOFF_FACTOR = float(os.getenv("RETRY_BACKOFF_FACWTOR", 0.3))


class ObservabilityClient:
    observability_server_url: str | None = None
    jaeger_service_account_token: str | None = None
    headers: dict | None = None
    session: Any | None = None

    def __init__(self, observability_url: str | None = None):
        # FIXME: this is always None because we don't use this env var anymore
        #   refactor this logic.
        self.observability_server_url = os.environ.get("JAEGER_URL", None)
        if self.observability_server_url is None:
            if observability_url is not None:
                self.observability_server_url = observability_url
            else:
                self.observability_server_url = f"http://localhost:{mcp_server_port}"

        logger.debug(f"observability endpoint is: {self.observability_server_url}")

        # This is almost always NOP because we don't have such setting
        self.jaeger_service_account_token = os.environ.get("GRAFANA_SERVICE_ACCOUNT_TOKEN", "NOP")

        logger.debug(f"url: {self.observability_server_url}, token: {self.jaeger_service_account_token}")

        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.jaeger_service_account_token}",
        }
        self.session = self.create_retrying_session()

    def create_retrying_session(self) -> requests.Session:
        session = requests.Session()

        retries = Retry(
            total=RETRY_TOTAL,
            backoff_factor=RETRY_BACKOFF_FACTOR,
            status_forcelist=[500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retries)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def make_request(self, method: str, url: str, **kwargs) -> requests.Response:
        try:
            response = self.session.request(method, url, headers=self.headers, timeout=REQUEST_TIMEOUT, **kwargs)
            response.raise_for_status()
            return response
        except requests.Timeout:
            logger.error(f"Request timed out after {REQUEST_TIMEOUT} seconds")
            raise
        except requests.RequestException as e:
            logger.error(f"Request failed: {e}")
            raise
