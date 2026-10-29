import logging
import time
from urllib.parse import parse_qsl, urlsplit

from kubernetes import client
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError
from urllib3.util import Retry

from sregym.conductor.oracles.mitigation import MitigationOracle

logger = logging.getLogger(__name__)


class IngressMisrouteMitigationOracle(MitigationOracle):
    """Require a healthy application and a real response through its Ingress."""

    ROUTE_SETTLE_SECONDS = 30
    POLL_SECONDS = 2
    REQUIRED_SUCCESSES = 3

    def _request_route(self) -> tuple[int, object]:
        # Proxy to the controller, not the application Service: NGINX must
        # apply the repaired path, rewrite, backend and port itself.
        route = urlsplit(self.problem.ingress_probe_path)
        configuration = client.Configuration.get_default_copy()
        # Observe this route's response, not a redirected endpoint. Retries
        # belong to the bounded observation loop below.
        configuration.retries = Retry(total=0, redirect=0, raise_on_redirect=False)
        with client.ApiClient(configuration) as api:
            body, status, _ = api.call_api(
                "/api/v1/namespaces/ingress-nginx/services/http:ingress-nginx-controller:80/proxy" + route.path,
                "GET",
                query_params=parse_qsl(route.query),
                header_params={"Accept": "application/json"},
                response_type="object",
                auth_settings=["BearerToken"],
                _return_http_data_only=False,
                _request_timeout=(5, 10),
            )
        return status, body

    def evaluate(self) -> dict:
        print("== Ingress Routing Evaluation ==")
        result = super().evaluate()
        if not result.get("success"):
            return result

        deadline = time.monotonic() + self.ROUTE_SETTLE_SECONDS
        successes = 0
        while True:
            try:
                status, payload = self._request_route()
                if status == 200 and self.problem.ingress_response_valid(payload):
                    # The controller can briefly serve its previous route
                    # after an edit. One cached configuration must not pass.
                    successes += 1
                    if successes >= self.REQUIRED_SUCCESSES:
                        logger.info("Ingress route %s serves the expected application response.", self.problem.path)
                        return result
                else:
                    successes = 0
                failure = self.fail("connectivity_probe_failed", path=self.problem.ingress_probe_path, status=status)
            except ApiException as exc:
                successes = 0
                if exc.status not in (404, 500, 502, 503, 504):
                    return self.fail_from_exception(exc)
                # These can be responses from NGINX or the Service proxy,
                # not necessarily evidence that the Kubernetes API failed.
                failure = self.fail(
                    "connectivity_probe_failed", path=self.problem.ingress_probe_path, status=exc.status
                )
            except (HTTPError, OSError, UnicodeError) as exc:
                successes = 0
                failure = self.fail("connectivity_probe_failed", path=self.problem.ingress_probe_path, error=str(exc))
            if time.monotonic() >= deadline:
                logger.info("Ingress route did not recover: %s", failure)
                return failure
            time.sleep(self.POLL_SECONDS)
