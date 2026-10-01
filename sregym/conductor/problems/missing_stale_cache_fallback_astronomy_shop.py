"""
Add a source-repair problem in which Astronomy Shop's recommendation service has no stale-cache or
graceful-degradation path when product-catalog is temporarily slow or unavailable.

A bounded upstream incident therefore becomes a recommendation outage even though previously fetched
data could safely serve requests.

Astronomy Shop's recommendation service fetches the product list from product-catalog on every request.
This problem overlays a version that calls product-catalog with a short timeout and caches the last
successful response in memory, but never falls back to the cache if a later request times out.

A Hidden Chaos Mesh delay on the product-catalog -> recommendation path (hidden from the agent) then turns
every recommendation into an error, while every pod stays Running and product-catalog itself looks healthy.

The fix requires changing the application code. The agent must update the overlaid `recommendation_server.py`
in its ConfigMap to serve the cached product list when product-catalog is unavailable, then restart the recommendation
Deployment. The mitigation oracle keeps the product-catalog impaired while evaluating the fix.
"""

import json
import random
import re
import shlex
import string
import time
from pathlib import Path

import yaml

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.missing_stale_cache_fallback_mitigation import MissingStaleCacheFallbackMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.generators.noise.manager import get_noise_manager
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

_ASSETS = Path(__file__).parent / "assets"

ROOT_CAUSE = (
    "The recommendation service calls product-catalog's ListProducts with a short timeout and re-raises on "
    "any error instead of serving the product list it already fetched and cached, so it has no graceful "
    "degradation. A bounded product-catalog slowdown therefore turns into a recommendation outage: "
    "ListRecommendations fails with the upstream deadline error while every pod stays Running and "
    "product-catalog itself is healthy. The fix is in recommendation_server.py. Keep a reasonable timeout and "
    "fall back to the cached, bounded-stale product list when product-catalog fails."
)

# Runs inside the recommendation pod with its own generated stubs.
# argv: count, interval_s, deadline_s, product ids to exclude as ONE comma-joined string.
_PROBE_SCRIPT = r"""
import json, sys, time
import grpc, demo_pb2, demo_pb2_grpc
count, interval, deadline, exclude = int(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
stub = demo_pb2_grpc.RecommendationServiceStub(grpc.insecure_channel("localhost:8080"))
for i in range(count):
    start = time.monotonic()
    try:
        reply = stub.ListRecommendations(demo_pb2.ListRecommendationsRequest(product_ids=[exclude]), timeout=deadline)
        print(json.dumps({"ok": True, "ms": (time.monotonic() - start) * 1000, "ids": list(reply.product_ids)}), flush=True)
    except grpc.RpcError as e:
        print(json.dumps({"ok": False, "ms": (time.monotonic() - start) * 1000, "code": e.code().name, "error": e.details()}), flush=True)
    if i < count - 1:
        time.sleep(interval)
"""

# Times one direct ListProducts call from the recommendation pod.
_UPSTREAM_SCRIPT = r"""
import os, time
import grpc, demo_pb2, demo_pb2_grpc
stub = demo_pb2_grpc.ProductCatalogServiceStub(grpc.insecure_channel(os.environ["PRODUCT_CATALOG_ADDR"]))
start = time.monotonic()
try:
    stub.ListProducts(demo_pb2.Empty(), timeout=10)
except grpc.RpcError:
    pass
print(f"{time.monotonic() - start:.3f}")
"""


class MissingStaleCacheFallbackAstronomyShop(Problem):
    RECOMMENDATION = "recommendation"
    PRODUCT_CATALOG = "product-catalog"
    SOURCE_PATH = "/app/recommendation_server.py"
    # The agent sees this ConfigMap (editing it is the repair path), so it carries an ordinary name.
    CONFIGMAP = "recommendation-server"
    # Hidden from the agent: k8s_proxy filters the chaos-mesh namespace and the chaos-mesh.org API group.
    CHAOS_NAMESPACE = "chaos-mesh"
    CHAOS_NAME = "catalog-latency"
    # A Schedule re-creates the NetworkChaos so pods restarted during the incident get the delay back
    # (25 s later). Overlapping runs cause brief latency spikes, which change no outcome.
    SCHEDULE_EVERY = "@every 30s"
    SCHEDULE_RUN = "40s"

    POSTGRES_DEPLOY = "postgresql"
    PG_SUPERUSER = "root"
    PG_PASSWORD = "otel"
    PG_DB = "otel"

    # Present in the buggy overlay only or in the reference fix only.
    BUGGY_MARKER = "CATALOG_TIMEOUT_SECONDS"
    FIXED_MARKER = "CATALOG_MAX_AGE_SECONDS"
    # A real catalog id which the service never recommends the product the caller is viewing.
    PROBE_PRODUCT = "OLJCESPC7Z"
    WARMUP_CALLS = 5

    def __init__(self, delay: str = "3s"):
        super().__init__(app=AstronomyShop())

        self.kubectl = KubeCtl()
        self.problem_id = "missing_stale_cache_fallback_astronomy_shop"
        self.faulty_service = [self.RECOMMENDATION]

        if not re.fullmatch(r"\d+(\.\d+)?s", delay):
            raise ValueError(f"delay must look like '3s', got {delay!r}")
        self.delay = delay
        # A ListProducts call slower than this proves the delay is live.
        self.delay_active_min_s = 0.8 * float(delay[:-1])

        self._buggy_source = (_ASSETS / "missing_stale_cache_fallback_recommendation.py").read_text()
        self._fixed_source = (_ASSETS / "missing_stale_cache_fallback_recommendation_fixed.py").read_text()

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.RECOMMENDATION}",
            namespace=self.namespace,
            description=ROOT_CAUSE,
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = MissingStaleCacheFallbackMitigationOracle(problem=self)

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self) -> bool:
        print("=== Fault Injection ===")
        try:
            self._overlay(self._buggy_source)
            if not self._overlay_live(present=self.BUGGY_MARKER, absent=self.FIXED_MARKER):
                raise RuntimeError(f"the recommendation overlay is not live at {self.SOURCE_PATH}")
            self.wait_until_answering()

            # Fill the in-memory product cache while product-catalog is still healthy
            warmup = self.probe_recommendations(self.WARMUP_CALLS, 0.5, 5.0)
            if not all(r["ok"] for r in warmup):
                raise RuntimeError(f"recommendation failed while warming its cache: {warmup}")

            self._ensure_chaos_mesh()
            self.apply_delay(keep_alive=True)
            if not self.wait_for_delay(active=True):
                raise RuntimeError(f"the {self.delay} product-catalog delay never became active")

            # The incident must be visible before the agent starts
            confirm = self.probe_recommendations(3, 1.0, 5.0)
            if any(r["ok"] for r in confirm):
                raise RuntimeError(f"recommendation still answers under the delay: {confirm}")
        except Exception:
            self._cleanup_after_failed_injection()
            raise

        print(f"Service: {self.RECOMMENDATION} | Namespace: {self.namespace} | upstream delay: {self.delay}")
        return True

    @mark_fault_injected
    def recover_fault(self) -> bool:
        print("=== Fault Recovery ===")
        self._delete_chaos()
        if not self._namespace_exists():
            print(f"Namespace '{self.namespace}' not present. Nothing more to recover.")
            return True

        # Recovery applies the reference fix rather than removing the overlay.
        # The stock image code has no timeout and no fallback, so it would fail the oracle,
        # which re-applies the delay while grading.
        self._overlay(self._fixed_source)
        if not self._overlay_live(present=self.FIXED_MARKER):
            raise RuntimeError(f"The fixed recommendation code is not live at {self.SOURCE_PATH}")

        self.wait_until_answering()
        print(f"Recovered: reference fix live in deployment/{self.RECOMMENDATION}, upstream delay removed.")
        return True

    def apply_delay(self, keep_alive: bool) -> None:
        """
        Delay product-catalog -> recommendation traffic.

        keep_alive=True creates a Schedule that keeps re-applying the delay (the incident).
        keep_alive=False creates one NetworkChaos with no duration (steady delay while the oracle grades).
        """
        self._delete_chaos()
        spec = {
            "action": "delay",
            "mode": "all",
            "selector": {
                "namespaces": [self.namespace],
                "labelSelectors": {"app.kubernetes.io/name": self.PRODUCT_CATALOG},
            },
            "direction": "to",
            "target": {
                "mode": "all",
                "selector": {
                    "namespaces": [self.namespace],
                    "labelSelectors": {"app.kubernetes.io/name": self.RECOMMENDATION},
                },
            },
            "delay": {"latency": self.delay},
        }
        metadata = {"name": self.CHAOS_NAME, "namespace": self.CHAOS_NAMESPACE}

        if keep_alive:
            manifest = {
                "apiVersion": "chaos-mesh.org/v1alpha1",
                "kind": "Schedule",
                "metadata": metadata,
                "spec": {
                    "schedule": self.SCHEDULE_EVERY,
                    "historyLimit": 2,
                    "concurrencyPolicy": "Allow",
                    "type": "NetworkChaos",
                    "networkChaos": {**spec, "duration": self.SCHEDULE_RUN},
                },
            }
        else:
            manifest = {
                "apiVersion": "chaos-mesh.org/v1alpha1",
                "kind": "NetworkChaos",
                "metadata": metadata,
                "spec": spec,
            }
        self.kubectl.exec_command_checked("kubectl apply -f -", input_data=yaml.safe_dump(manifest, sort_keys=False))

    def remove_delay(self) -> None:
        self._delete_chaos()
        if not self.wait_for_delay(active=False):
            raise RuntimeError("The product-catalog delay is still active after removing it")

    def delay_active(self) -> bool:
        """
        True when a direct ListProducts call from the recommendation pod is as slow as the injected delay
        """
        try:
            out = self._exec_python(_UPSTREAM_SCRIPT, [], timeout=30)
            return float(out.strip().splitlines()[-1]) >= self.delay_active_min_s
        except (RuntimeError, ValueError, IndexError):
            return False

    def wait_for_delay(self, active: bool, timeout_s: float = 90) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.delay_active() == active:
                return True
            time.sleep(3)
        return False

    def probe_recommendations(
        self, count: int, interval_s: float, deadline_s: float, exclude_ids: list[str] | None = None
    ) -> list[dict]:
        """
        Call ListRecommendations from inside the recommendation pod.

        Each result: {"ok": bool, "ms": float, "ids": [...]} or {"ok": False, "ms", "code", "error"}.
        exclude_ids travel as ONE comma-joined string, because the service does ''.join(ids).split(',').
        """
        exclude = ",".join(exclude_ids) if exclude_ids else self.PROBE_PRODUCT
        args = [str(count), str(interval_s), str(deadline_s), exclude]

        try:
            out = self._exec_python(_PROBE_SCRIPT, args, timeout=count * (interval_s + deadline_s) + 60)
        except RuntimeError as exc:
            return [{"ok": False, "ms": 0.0, "code": "EXEC_FAILED", "error": str(exc)}]

        results = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
        return results or [{"ok": False, "ms": 0.0, "code": "NO_OUTPUT", "error": out[-500:]}]

    def wait_until_answering(self, timeout_s: float = 90) -> None:
        """
        Wait until the gRPC port accepts calls. `rollout status` finishes 5s before it does
        """
        deadline = time.monotonic() + timeout_s
        last = None
        while time.monotonic() < deadline:
            last = self.probe_recommendations(1, 0, 5.0)[0]
            if last["ok"]:
                return
            # Any application-level answer counts (the buggy code answers with an error under the delay).
            # Only "no pod to exec into" and "nothing listening on localhost:8080" mean keep waiting.
            no_pod = last["code"] in ("EXEC_FAILED", "NO_OUTPUT")
            not_listening = last["code"] == "UNAVAILABLE" and "127.0.0.1" in last.get("error", "")
            if not (no_pod or not_listening):
                return
            time.sleep(2)
        raise RuntimeError(f"Recommendation is not answering after {timeout_s:.0f}s: {last}")

    def catalog_ids(self) -> list[str]:
        out = self._psql("SELECT id FROM catalog.products ORDER BY id")
        return [line.strip() for line in out.splitlines() if line.strip()]

    def insert_probe_product(self) -> str:
        """
        Insert a new product the way the catalog would grow, and return its id
        """
        existing = set(self.catalog_ids())
        while True:
            product_id = "".join(random.choices(string.ascii_uppercase + string.digits, k=10))
            if product_id not in existing:
                break

        self._psql(
            "INSERT INTO catalog.products "
            "(id, name, description, picture, price_currency_code, price_units, price_nanos, categories) "
            f"VALUES ('{product_id}', 'Lunar Filter', 'Neutral density filter for bright lunar observation.', "
            "'LunarFilter.jpg', 'USD', 19, 990000000, 'accessories')"
        )
        return product_id

    def delete_product(self, product_id: str) -> None:
        if not re.fullmatch(r"[A-Z0-9]{10}", product_id):
            raise ValueError(f"unexpected product id {product_id!r}")
        self._psql(f"DELETE FROM catalog.products WHERE id = '{product_id}'")

    """ Helpers """

    def _overlay(self, source: str) -> None:
        ApplicationFaultInjector(namespace=self.namespace).inject_source_file_override(
            deployment_name=self.RECOMMENDATION,
            source_path=self.SOURCE_PATH,
            replacement_content=source,
            configmap_name=self.CONFIGMAP,
            container_name=self.RECOMMENDATION,
        )

    def _remove_overlay(self) -> None:
        ApplicationFaultInjector(namespace=self.namespace).recover_source_file_override(
            deployment_name=self.RECOMMENDATION,
            source_path=self.SOURCE_PATH,
            configmap_name=self.CONFIGMAP,
            container_name=self.RECOMMENDATION,
        )

    def _overlay_live(self, present: str, absent: str | None = None) -> bool:
        try:
            pod = self._recommendation_pod()
            text = self.kubectl.exec_command_checked(
                f"kubectl exec -n {self.namespace} {pod} -c {self.RECOMMENDATION} -- cat {self.SOURCE_PATH}",
                timeout=30,
            )
        except RuntimeError:
            return False
        return present in text and (absent is None or absent not in text)

    def _recommendation_pod(self) -> str:
        """
        Newest running, non-terminating recommendation pod.

        `kubectl exec deploy/recommendation` can land on a pod that is still terminating after a rollout.
        """
        pods = self.kubectl.core_v1_api.list_namespaced_pod(
            self.namespace, label_selector=f"app.kubernetes.io/name={self.RECOMMENDATION}"
        ).items
        live = [p for p in pods if p.metadata.deletion_timestamp is None and p.status.phase == "Running"]
        if not live:
            raise RuntimeError("No running recommendation pod")
        return max(live, key=lambda p: p.metadata.creation_timestamp).metadata.name

    def _exec_python(self, script: str, args: list[str], timeout: float) -> str:
        pod = self._recommendation_pod()
        argv = " ".join(shlex.quote(a) for a in args)
        command = f"kubectl exec -i -n {self.namespace} {pod} -c {self.RECOMMENDATION} -- /venv/bin/python - {argv}"
        return self.kubectl.exec_command_checked(command, input_data=script, timeout=timeout)

    def _psql(self, query: str) -> str:
        command = (
            f"kubectl exec -n {self.namespace} deploy/{self.POSTGRES_DEPLOY} -- "
            f"env PGPASSWORD={self.PG_PASSWORD} psql -U {self.PG_SUPERUSER} -d {self.PG_DB} "
            f"-v ON_ERROR_STOP=1 -tA -c {shlex.quote(query)}"
        )
        return self.kubectl.exec_command_checked(command, timeout=60)

    def _ensure_chaos_mesh(self) -> None:
        # Same installer SREGym's noise injection uses
        # The chaos-mesh namespace survives between problems.
        get_noise_manager()._ensure_chaos_mesh_installed()
        self.kubectl.exec_command_checked(
            "kubectl get crd networkchaos.chaos-mesh.org schedules.chaos-mesh.org", timeout=30
        )

    def _delete_chaos(self) -> None:
        """
        Delete our Schedule and NetworkChaos, then wait until no child NetworkChaos of ours is left
        """
        self.kubectl.exec_command(
            f"kubectl delete schedule,networkchaos {self.CHAOS_NAME} -n {self.CHAOS_NAMESPACE} "
            "--ignore-not-found --wait=true"
        )
        deadline = time.monotonic() + 60

        while time.monotonic() < deadline:
            names = self.kubectl.exec_command(f"kubectl get networkchaos -n {self.CHAOS_NAMESPACE} -o name")
            if not any(f"/{self.CHAOS_NAME}" in line for line in names.splitlines()):
                return
            time.sleep(2)
        print(f"Warning: NetworkChaos objects named {self.CHAOS_NAME}* still present after 60s")

    def _namespace_exists(self) -> bool:
        out = self.kubectl.exec_command(f"kubectl get namespace {self.namespace} --no-headers --ignore-not-found")
        return bool(out.strip())

    def _cleanup_after_failed_injection(self) -> None:
        for step in (self._delete_chaos, self._remove_overlay):
            try:
                step()
            except Exception as exc:
                print(f"[Cleanup] {step.__name__} failed: {exc}")
