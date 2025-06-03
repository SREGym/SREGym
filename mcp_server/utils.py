import logging
import os
from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", 120))
RETRY_TOTAL = int(os.getenv("RETRY_TOTAL", 3))
RETRY_BACKOFF_FACTOR = float(os.getenv("RETRY_BACKOFF_FACTOR", 0.3))


class ObservabilityClient:
    jaeger_url: Optional[str] = None
    jaeger_service_account_token: Optional[str] = None
    headers: Optional[Dict] = None
    session: Optional[Any] = None

    def __init__(self, jaeger_url: Optional[str] = None):
        self.jaeger_url = os.environ.get("OBSERVABILITY_STACK_URL", None)
        if self.jaeger_url is None:
            if jaeger_url is not None:
                self.jaeger_url = jaeger_url
            else:
                self.jaeger_url = "http://localhost:8000"

        logger.info(f"jaeger url: {self.jaeger_url}")

        self.jaeger_service_account_token = os.environ.get("GRAFANA_SERVICE_ACCOUNT_TOKEN", "NOP")

        logger.debug("url: {g}, token: {t}".format(g=self.jaeger_url, t=self.jaeger_service_account_token))

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
