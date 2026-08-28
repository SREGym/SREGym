"""GOMEMLIMIT unit-mismatch fault for Astronomy Shop.

Real-world story
----------------
Unit-mismatch failures are the software analogue of the Mars Climate Orbiter:
two sides of an interface agree on a number and disagree on its unit. Go's
GOMEMLIMIT accepts the IEC byte suffixes B, KiB, MiB, GiB and TiB. The
SI-looking `MB` that operators habitually type, and the `Mi` that Kubernetes
uses two lines away in the same manifest, are both rejected by the runtime
before main() runs, so the process never starts.

The failure is not memory pressure. The requested quantity is reasonable, the
node has capacity, and the Kubernetes limits are untouched. Diagnosis means
correlating a startup parse error with one environment variable rather than
adding memory or chasing node pressure.

SREGym simulation
-----------------
1. inject_fault: rewrite the target's GOMEMLIMIT to the SI-suffixed form of
   whatever the chart currently sets, replacing the pod deterministically so no
   old Ready replica survives to mask the crash-loop, then verify the new pod is
   crash-looping *with a malformed-GOMEMLIMIT error* rather than for any other
   reason.
2. recover_fault: restore the captured pod template verbatim and wait for the
   rollout to complete.

Valid agent mitigations (all accepted by the oracle)
- any value the Go runtime accepts with reasonable capacity, not just the
  original string: `16MiB`, `32MiB`, `16777216`, `1GiB` all pass
- removing the override entirely, which leaves the runtime default
- setting it to `off`, which disables the soft limit
"""

import copy
import logging
import time

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.unit_mismatch_gomemlimit_mitigation import (
    PROBE_ID_PLACEHOLDER,
    UnitMismatchGomemlimitMitigation,
    parse_go_memory_limit,
)
from sregym.conductor.problems.base import Problem
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

# The values the chart ships today. Injection asserts the live deployment still
# matches, so the root-cause text handed to the judge can never drift away from
# what was actually injected.
_EXPECTED_CHART_VALUES = {
    "checkout": "16MiB",
    "product-catalog": "16MiB",
}

# checkout does not serve gRPC reflection, so grpcurl is given just enough of
# the demo's schema to place a real order. product-catalog does serve
# reflection and needs none of this.
_CHECKOUT_PROTO = """syntax = "proto3";
package oteldemo;

service CartService {
  rpc AddItem(AddItemRequest) returns (Empty) {}
}

service CheckoutService {
  rpc PlaceOrder(PlaceOrderRequest) returns (PlaceOrderResponse) {}
}

message Empty {}
message CartItem { string product_id = 1; int32 quantity = 2; }
message AddItemRequest { string user_id = 1; CartItem item = 2; }
message Address {
  string street_address = 1;
  string city = 2;
  string state = 3;
  string country = 4;
  string zip_code = 5;
}
message CreditCardInfo {
  string credit_card_number = 1;
  int32 credit_card_cvv = 2;
  int32 credit_card_expiration_year = 3;
  int32 credit_card_expiration_month = 4;
}
message PlaceOrderRequest {
  string user_id = 1;
  string user_currency = 2;
  Address address = 3;
  string email = 5;
  CreditCardInfo credit_card = 6;
}
message Money { string currency_code = 1; int64 units = 2; int32 nanos = 3; }
message OrderItem { CartItem item = 1; Money cost = 2; }
message OrderResult {
  string order_id = 1;
  string shipping_tracking_id = 2;
  Money shipping_cost = 3;
  Address shipping_address = 4;
  repeated OrderItem items = 5;
}
message PlaceOrderResponse { OrderResult order = 1; }
"""

_PROBE_PRODUCT_ID = "OLJCESPC7Z"


def _checkout_probe() -> dict:
    """Place a genuine order: add an item to a cart, then check out."""
    return {
        "service": "checkout",
        "rpc": "oteldemo.CheckoutService/PlaceOrder",
        "proto": _CHECKOUT_PROTO,
        "success_token": "orderId",
        "prerequisite": {
            "service": "cart",
            "rpc": "oteldemo.CartService/AddItem",
            "payload": {
                "user_id": PROBE_ID_PLACEHOLDER,
                "item": {"product_id": _PROBE_PRODUCT_ID, "quantity": 1},
            },
        },
        "payload": {
            "user_id": PROBE_ID_PLACEHOLDER,
            "user_currency": "USD",
            "address": {
                "street_address": "1600 Amphitheatre Parkway",
                "city": "Mountain View",
                "state": "CA",
                "country": "USA",
                "zip_code": "94043",
            },
            "email": "probe@example.com",
            "credit_card": {
                "credit_card_number": "4432-8015-6152-0454",
                "credit_card_cvv": 672,
                "credit_card_expiration_year": 2030,
                "credit_card_expiration_month": 1,
            },
        },
    }


def _product_catalog_probe() -> dict:
    """Read the catalog back through the service's own API."""
    return {
        "service": "product-catalog",
        "rpc": "oteldemo.ProductCatalogService/ListProducts",
        "proto": None,
        "success_token": _PROBE_PRODUCT_ID,
        "prerequisite": None,
        "payload": {},
    }


_PROBES = {
    "checkout": _checkout_probe,
    "product-catalog": _product_catalog_probe,
}


class UnitMismatchGomemlimitAstronomyShop(Problem):
    """Set GOMEMLIMIT to an SI byte suffix the Go runtime refuses to parse."""

    _CRASH_TIMEOUT_SECONDS = 180
    _ROLLOUT_TIMEOUT_SECONDS = 180
    _POLL_INTERVAL_SECONDS = 5
    _PARSE_ERROR_MARKER = "malformed GOMEMLIMIT"

    def __init__(self, faulty_service: str = "checkout"):
        """Configure the Astronomy Shop GOMEMLIMIT unit-mismatch problem."""
        if faulty_service not in _EXPECTED_CHART_VALUES:
            raise ValueError(
                f"UnitMismatchGomemlimitAstronomyShop has no variant for '{faulty_service}'; "
                f"known: {sorted(_EXPECTED_CHART_VALUES)}"
            )

        self.app = AstronomyShop()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)
        self.kubectl = KubeCtl()

        self.faulty_service = faulty_service
        self.env_var = "GOMEMLIMIT"
        self.service_port = 8080
        self.expected_chart_value = _EXPECTED_CHART_VALUES[faulty_service]
        # The SI-suffixed form of the shipped value: same number, wrong unit.
        self.wrong_value = self.expected_chart_value.replace("MiB", "MB")
        self._baseline_template = None
        self._baseline_replicas: int | None = None

        # A real request through the service's own API. Neither target defines a
        # readiness probe, so a Running pod proves nothing without this.
        self.probe = _PROBES[faulty_service]()

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"The {self.env_var} environment variable on {self.faulty_service} is set to "
                f"'{self.wrong_value}'. The Go runtime only accepts the IEC byte suffixes B, KiB, MiB, GiB and TiB, "
                f"so the SI-style '{self.wrong_value[-2:]}' suffix is rejected during runtime initialisation with "
                "'fatal error: malformed GOMEMLIMIT' and the process exits before it can serve. The pod therefore "
                f"crash-loops while node memory, pod memory limits and the rest of the deployment stay healthy: this "
                f"is a unit-mismatch configuration error, not memory exhaustion. The value should be "
                f"'{self.expected_chart_value}' or any other quantity the runtime accepts."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.app.create_workload()
        self.mitigation_oracle = UnitMismatchGomemlimitMitigation(problem=self)

    # ------------------------------------------------------------------
    # Deployment helpers
    # ------------------------------------------------------------------

    def _target_container(self, deployment):
        containers = deployment.spec.template.spec.containers
        return next(
            (item for item in containers if item.name == self.faulty_service),
            containers[0],
        )

    def _configured_limit(self, deployment) -> str | None:
        container = self._target_container(deployment)
        for env in container.env or []:
            if env.name == self.env_var:
                return env.value
        return None

    def _set_limit(self, value: str) -> None:
        """Patch the target's GOMEMLIMIT, replacing the pod deterministically.

        Scaling to zero before the template lands guarantees the healthy pod is
        gone: a default RollingUpdate would keep it Ready and hide the
        crash-loop behind it.
        """
        deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        container_name = self._target_container(deployment).name
        replicas = self._baseline_replicas if self._baseline_replicas is not None else (deployment.spec.replicas or 1)

        self.kubectl.patch_deployment(
            name=self.faulty_service,
            namespace=self.namespace,
            patch_body={"spec": {"replicas": 0}},
        )
        self.kubectl.patch_deployment(
            name=self.faulty_service,
            namespace=self.namespace,
            patch_body={
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [{"name": container_name, "env": [{"name": self.env_var, "value": value}]}]
                        }
                    }
                }
            },
        )
        self.kubectl.patch_deployment(
            name=self.faulty_service,
            namespace=self.namespace,
            patch_body={"spec": {"replicas": replicas}},
        )

    def _target_pods(self) -> list:
        selector = {"opentelemetry.io/name": self.faulty_service}
        return [
            pod
            for pod in self.kubectl.list_pods(self.namespace).items
            if pod.metadata.deletion_timestamp is None
            and all((pod.metadata.labels or {}).get(key) == value for key, value in selector.items())
        ]

    def _pod_logs(self, pod_name: str) -> str:
        """Read a crash-looping pod's output, falling back to the previous attempt."""
        for previous in (False, True):
            try:
                logs = self.kubectl.core_v1_api.read_namespaced_pod_log(
                    name=pod_name,
                    namespace=self.namespace,
                    container=self.faulty_service,
                    previous=previous,
                    tail_lines=40,
                )
            except Exception:
                continue
            if logs and self._PARSE_ERROR_MARKER in logs:
                return logs
        return ""

    def _wait_for_parse_error_crashloop(self) -> None:
        """Block until a target pod is crash-looping *because of* the bad value.

        Checking the reason rather than merely the symptom keeps this honest on
        clusters where the service can also die of ambient memory pressure.
        """
        deadline = time.monotonic() + self._CRASH_TIMEOUT_SECONDS
        last_seen = "no target pod observed"
        while time.monotonic() < deadline:
            for pod in self._target_pods():
                for status in pod.status.container_statuses or []:
                    waiting = status.state.waiting.reason if status.state.waiting else None
                    terminated = status.state.terminated.reason if status.state.terminated else None
                    last_seen = (
                        f"{pod.metadata.name}: waiting={waiting} terminated={terminated} "
                        f"restarts={status.restart_count}"
                    )
                    crashing = waiting == "CrashLoopBackOff" or (status.restart_count or 0) >= 1 or terminated
                    if not crashing:
                        continue
                    if self._PARSE_ERROR_MARKER in self._pod_logs(pod.metadata.name):
                        logger.info("[gomemlimit] fault confirmed live: %s", last_seen)
                        return
            time.sleep(self._POLL_INTERVAL_SECONDS)
        raise RuntimeError(
            f"{self.faulty_service} did not crash-loop with a {self._PARSE_ERROR_MARKER} error "
            f"within {self._CRASH_TIMEOUT_SECONDS}s; last={last_seen}"
        )

    def _wait_for_healthy_rollout(self) -> None:
        self.kubectl.exec_command(
            f"kubectl rollout status deployment/{self.faulty_service} -n {self.namespace} "
            f"--timeout={self._ROLLOUT_TIMEOUT_SECONDS}s"
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @mark_fault_injected
    def inject_fault(self):
        """Rewrite GOMEMLIMIT to an SI suffix the Go runtime rejects."""
        print("== Fault Injection ==")

        deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        live_value = self._configured_limit(deployment)
        if live_value != self.expected_chart_value:
            raise RuntimeError(
                f"{self.faulty_service} has {self.env_var}={live_value!r}, expected "
                f"{self.expected_chart_value!r}; the root-cause text would not match the injected fault"
            )
        if parse_go_memory_limit(self.wrong_value) is not None:
            raise RuntimeError(f"{self.wrong_value!r} is a value the Go runtime accepts; it cannot break the service")

        self._baseline_template = copy.deepcopy(deployment.spec.template)
        self._baseline_replicas = deployment.spec.replicas or 1

        logger.info("[gomemlimit] setting %s=%s on %s", self.env_var, self.wrong_value, self.faulty_service)
        self._set_limit(self.wrong_value)
        self._wait_for_parse_error_crashloop()

        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        """Restore the captured pod template and wait for a healthy rollout."""
        print("== Fault Recovery ==")

        if self._baseline_template is None:
            # A fresh problem instance recovering a cluster it did not inject.
            self._set_limit(self.expected_chart_value)
        else:
            deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
            deployment.spec.template = copy.deepcopy(self._baseline_template)
            deployment.spec.replicas = self._baseline_replicas
            self.kubectl.apps_v1_api.replace_namespaced_deployment(
                name=self.faulty_service,
                namespace=self.namespace,
                body=deployment,
            )

        self._wait_for_healthy_rollout()
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
