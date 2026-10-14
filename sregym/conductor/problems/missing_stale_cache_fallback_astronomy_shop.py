"""
Add a source-repair problem in which Astronomy Shop's recommendation service has no stale-cache or
graceful-degradation path when product-catalog is temporarily slow or unavailable.

A bounded upstream incident therefore becomes a recommendation outage even though previously fetched
data could safely serve requests.

Astronomy Shop's recommendation service fetches the product list from product-catalog on every request.
This problem overlays a version that calls product-catalog with a short timeout and caches the last
successful response in memory, but never falls back to the cache if a later request times out.

A Chaos Mesh delay on the product-catalog -> recommendation path (hidden from the agent) then turns
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
from kubernetes.client import ApiClient
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.missing_stale_cache_fallback_mitigation import (
    MissingStaleCacheFallbackMitigationOracle,
    ProbeUnavailable,
    UpstreamRecoveryError,
)
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

# Calls ListRecommendations, normally from a short-lived client pod built from the stock recommendation image (it
# has the generated stubs) through the recommendation Service, the way the frontend does. A new channel per call is
# a new connection, so the Service picks a backend each time.
# argv: count, interval_s, deadline_s, product ids to exclude (comma-joined), address.
_PROBE_SCRIPT = r"""
import json, sys, time
sys.path.insert(0, "/app")
import grpc, demo_pb2, demo_pb2_grpc
count, interval, deadline, exclude, address = int(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3]), sys.argv[4], sys.argv[5]
# One id per element with "," elements in between. That reads the same whether the service does
# ''.join(ids).split(','), reads the list as it is, or splits each element.
product_ids = []
for product_id in filter(None, exclude.split(",")):
    product_ids += [product_id, ","]
product_ids = product_ids[:-1]
for i in range(count):
    channel = grpc.insecure_channel(address)
    stub = demo_pb2_grpc.RecommendationServiceStub(channel)
    start = time.monotonic()
    try:
        reply = stub.ListRecommendations(demo_pb2.ListRecommendationsRequest(product_ids=product_ids), timeout=deadline)
        print(json.dumps({"ok": True, "ms": (time.monotonic() - start) * 1000, "ids": list(reply.product_ids)}), flush=True)
    except grpc.RpcError as e:
        print(json.dumps({"ok": False, "ms": (time.monotonic() - start) * 1000, "code": e.code().name, "error": e.details()}), flush=True)
    channel.close()
    if i < count - 1:
        time.sleep(interval)
"""

# Times one direct ListProducts call from the recommendation pod.
# argv: address, timeout_s.
_UPSTREAM_SCRIPT = r"""
import json, sys, time
sys.path.insert(0, "/app")
import grpc, demo_pb2, demo_pb2_grpc
address, timeout = sys.argv[1], float(sys.argv[2])
stub = demo_pb2_grpc.ProductCatalogServiceStub(grpc.insecure_channel(address))
start, code = time.monotonic(), "OK"
try:
    stub.ListProducts(demo_pb2.Empty(), timeout=timeout)
except grpc.RpcError as e:
    code = e.code().name
print(json.dumps({"addr": address, "s": round(time.monotonic() - start, 3), "code": code}))
"""

# Asks the frontend for recommendations the way the shop's product page does, so the request reaching recommendation
# has the frontend's own client, metadata and shape. The frontend answers with up to 4 product objects.
# argv: count, interval_s, deadline_s, product id to exclude, frontend address.
_FRONTEND_SCRIPT = r"""
import json, sys, time, urllib.error, urllib.request
count, interval, deadline, exclude, address = int(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3]), sys.argv[4], sys.argv[5]
url = f"http://{address}/api/recommendations?productIds={exclude}&sessionId=check&currencyCode=USD"
for i in range(count):
    start = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=deadline) as reply:
            ids = [p["id"] for p in json.loads(reply.read())]
        print(json.dumps({"ok": True, "ms": (time.monotonic() - start) * 1000, "ids": ids}), flush=True)
    except urllib.error.HTTPError as e:
        print(json.dumps({"ok": False, "ms": (time.monotonic() - start) * 1000, "code": f"HTTP_{e.code}", "error": str(e)}), flush=True)
    except Exception as e:
        print(json.dumps({"ok": False, "ms": (time.monotonic() - start) * 1000, "code": "UNAVAILABLE", "error": str(e)}), flush=True)
    if i < count - 1:
        time.sleep(interval)
"""


class MissingStaleCacheFallbackAstronomyShop(Problem):
    RECOMMENDATION = "recommendation"
    PRODUCT_CATALOG = "product-catalog"
    # Call recommendation through its Service. Confirm the delay at product-catalog's pod addresses.
    RECOMMENDATION_ADDR = "recommendation:8080"
    LOCAL_ADDR = "localhost:8080"
    UPSTREAM_PORT = 8080
    # The client pod runs the stock image with no app labels, so neither the Service nor the delay selects it,
    # and nothing the agent changed in the recommendation container can touch the probe.
    CLIENT_CONTAINER = "client"
    SOURCE_PATH = "/app/recommendation_server.py"
    # The agent sees this ConfigMap (editing it is the repair path), so it carries an ordinary name.
    CONFIGMAP = "recommendation-server"
    # Deployment selector label. Unlike app.kubernetes.io/name it cannot be removed from running pods.
    SELECTOR_LABEL = "opentelemetry.io/name"
    # Chaos objects live in the app namespace, so they are deleted with it.
    # Hidden from the agent: k8s_proxy filters the chaos-mesh.org API group in every namespace.
    CHAOS_NAME = "catalog-latency"
    CHAOS_RESOURCES = "schedules.chaos-mesh.org,networkchaos.chaos-mesh.org"
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
    # Below this the oracle's latency limit (delay - 0.25 s) leaves little room for a 1 s timeout fallback.
    MIN_DELAY_S = 2.0

    def __init__(self, delay: str = "3s"):
        super().__init__(app=AstronomyShop())

        self.kubectl = KubeCtl()
        self.faulty_service = [self.RECOMMENDATION]

        self.delay = delay
        self.delay_seconds = self._parse_delay(delay)
        # A ListProducts call slower than this proves the delay is live.
        self.delay_active_min_s = 0.8 * self.delay_seconds

        # Image for the probe client, recorded at injection before the agent can change the Deployment.
        self._client_image: str | None = None
        self._client: str | None = None

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
            self._client_image = self._deployment_image(self.RECOMMENDATION)
            self._overlay(self._buggy_source)
            if not self._overlay_live(present=self.BUGGY_MARKER, absent=self.FIXED_MARKER):
                raise RuntimeError(f"the recommendation overlay is not live at {self.SOURCE_PATH}")
            self.wait_until_answering()

            # Fill the in-memory product cache while product-catalog is still healthy
            self._warm_cache()

            self._ensure_chaos_mesh()
            self.apply_delay(keep_alive=True)
            if not self.wait_for_delay(active=True):
                raise RuntimeError(f"the {self.delay} product-catalog delay never became active")

            # The incident must be visible before the agent starts: real RPC answers that carry the upstream
            # deadline error. A probe that could not run proves nothing.
            confirm = self.probe_recommendations(3, 1.0, self.delay_seconds + 2)
            if not self._incident_confirmed(confirm):
                raise RuntimeError(f"recommendation calls did not show the injected failure: {confirm}")
        except Exception:
            self._cleanup_after_failed_injection()
            raise
        finally:
            self.release_client()

        print(f"Service: {self.RECOMMENDATION} | Namespace: {self.namespace} | upstream delay: {self.delay}")
        return True

    @mark_fault_injected(strict=True)
    def recover_fault(self) -> bool:
        print("=== Fault Recovery ===")
        # Leftover chaos would keep delaying pods after this problem, so a failed delete stops recovery loudly.
        self._delete_chaos()
        if not self._namespace_exists():
            print(f"Namespace '{self.namespace}' not present. Nothing more to recover.")
            return True

        # Recovery applies the reference fix rather than removing the overlay.
        # The stock image code has no timeout and no fallback, so it would fail the oracle,
        # which re-applies the delay while grading.
        # This part is best effort: the agent may have scaled recommendation to 0 or deleted it, and failing the
        # teardown for that would abort the whole run. validate_problem still catches a fix that isn't live,
        # because the oracle must pass after recovery.
        try:
            self._overlay(self._fixed_source)
            if not self._overlay_live(present=self.FIXED_MARKER):
                raise RuntimeError(f"the fixed recommendation code is not live at {self.SOURCE_PATH}")
            self.wait_until_answering()
        except Exception as exc:
            print(f"ERROR: could not re-apply the reference fix: {exc}")
            return False
        finally:
            self.release_client()

        print(f"Recovered: reference fix live in deployment/{self.RECOMMENDATION}, upstream delay removed.")
        return True

    def apply_delay(self, keep_alive: bool, latency: str | None = None) -> None:
        """
        Delay product-catalog -> recommendation traffic.

        keep_alive=True creates a Schedule that keeps re-applying the delay (the incident).
        keep_alive=False creates one NetworkChaos with no duration (steady delay while the oracle grades).
        latency overrides the problem's delay (the oracle uses a much longer one to tell whether fresh data
        is reaching recommendation some other way).
        """
        self._delete_chaos()
        spec = {
            "action": "delay",
            "mode": "all",
            "selector": {
                "namespaces": [self.namespace],
                "labelSelectors": {self.SELECTOR_LABEL: self.PRODUCT_CATALOG},
            },
            "direction": "to",
            "target": {
                "mode": "all",
                "selector": {
                    "namespaces": [self.namespace],
                    "labelSelectors": {self.SELECTOR_LABEL: self.RECOMMENDATION},
                },
            },
            "delay": {"latency": latency or self.delay},
        }
        metadata = {"name": self.CHAOS_NAME, "namespace": self.namespace}

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
            raise UpstreamRecoveryError("product-catalog did not answer quickly after removing the delay")

    def incident_running(self) -> bool:
        if not self._chaos_crds_installed():
            return False
        out = self.kubectl.exec_command_checked(
            f"kubectl get schedules.chaos-mesh.org {self.CHAOS_NAME} -n {self.namespace} -o name --ignore-not-found",
            timeout=30,
        )
        return self.CHAOS_NAME in out

    def measure_upstream(self) -> dict | None:
        """
        Time one ListProducts call from the recommendation pod to a product-catalog pod: {"addr", "s", "code"},
        or None if that cannot be done.

        It goes to the pod IP, not the name, so a hostAliases or DNS change in the recommendation pod can't make a
        product-catalog copy look like the delayed original.
        """
        timeout_s = 3 * self.delay_seconds + 5
        try:
            addresses = self.upstream_addrs()
            if not addresses:
                return None
            out = self._exec_in_rollout(_UPSTREAM_SCRIPT, [addresses[0], str(timeout_s)], timeout=timeout_s + 30)
            return json.loads(out.strip().splitlines()[-1])
        except (RuntimeError, ValueError, IndexError):
            return None

    def delay_active(self) -> bool | None:
        """
        True when a direct call to product-catalog is as slow as the injected delay, False when it is fast
        and answers OK. None when that cannot be told (exec failed, or a fast error).
        """
        upstream = self.measure_upstream()
        if upstream is None:
            return None
        if upstream["s"] >= self.delay_active_min_s:
            return True
        return False if upstream["code"] == "OK" else None

    def wait_for_delay(self, active: bool, timeout_s: float = 90) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.delay_active() is active:
                return True
            time.sleep(3)
        return False

    def probe_recommendations(
        self,
        count: int,
        interval_s: float,
        deadline_s: float,
        exclude_ids: list[str] | None = None,
        pod: str | None = None,
    ) -> list[dict]:
        """
        Call ListRecommendations through the recommendation Service from the client pod, or, with `pod`, call that
        recommendation pod directly on its own port.

        Each result: {"ok": bool, "ms": float, "ids": [...]} or {"ok": False, "ms", "code", "error"}.
        """
        exclude = ",".join(exclude_ids) if exclude_ids else self.PROBE_PRODUCT
        address = self.LOCAL_ADDR if pod else self.RECOMMENDATION_ADDR
        args = [str(count), str(interval_s), str(deadline_s), exclude, address]
        timeout = count * (interval_s + deadline_s) + 60

        try:
            if pod:
                out = self._exec_in_rollout(_PROBE_SCRIPT, args, timeout=timeout, pod=pod)
            else:
                out = self._exec_in_client(_PROBE_SCRIPT, args, timeout=timeout)
        except RuntimeError as exc:
            return [{"ok": False, "ms": 0.0, "code": "EXEC_FAILED", "error": str(exc)}]

        results = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
        return results or [{"ok": False, "ms": 0.0, "code": "NO_OUTPUT", "error": out[-500:]}]

    def probe_frontend(self, count: int, interval_s: float, deadline_s: float, exclude_id: str) -> list[dict]:
        """
        Ask for recommendations through the application's entry point, from the client pod.
        Same result shape as probe_recommendations(), with the frontend's product ids.
        """
        address = f"{self.app.frontend_service}:{self.app.frontend_port}"
        args = [str(count), str(interval_s), str(deadline_s), exclude_id, address]
        try:
            out = self._exec_in_client(_FRONTEND_SCRIPT, args, timeout=count * (interval_s + deadline_s) + 60)
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
            # Only "no pod to exec into" and "nothing behind the Service yet" mean keep waiting.
            no_pod = last["code"] in ("EXEC_FAILED", "NO_OUTPUT")
            error = last.get("error") or ""
            not_listening = last["code"] == "UNAVAILABLE" and "Connection refused" in error
            if not (no_pod or not_listening):
                return
            time.sleep(2)
        if last and last["code"] in ("EXEC_FAILED", "NO_OUTPUT"):
            raise ProbeUnavailable(f"could not probe recommendation for {timeout_s:.0f}s: {last}")
        raise RuntimeError(f"Recommendation is not answering after {timeout_s:.0f}s: {last}")

    def rollout_pods(self) -> list[str]:
        """
        Running pods of the recommendation Deployment's current ReplicaSet, oldest first.
        """
        return [p.metadata.name for p in self._rollout_pod_objects()]

    def rollout_state(self) -> dict[str, int]:
        """
        Pod uid -> container restarts, for every running pod of the current recommendation rollout.
        """
        return {
            p.metadata.uid: sum(c.restart_count for c in p.status.container_statuses or [])
            for p in self._rollout_pod_objects()
        }

    def routing_specs(self) -> dict:
        """
        How traffic reaches recommendation and product-catalog, which the delay is built on: both Services'
        selector, ports and session affinity, and how the recommendation pods resolve names.
        """
        specs = {}
        for name in (self.RECOMMENDATION, self.PRODUCT_CATALOG):
            try:
                svc = self.kubectl.core_v1_api.read_namespaced_service(name, self.namespace)
            except ApiException as exc:
                if exc.status == 404:
                    specs[name] = None
                    continue
                raise RuntimeError(f"could not read Service {name}: {exc.status} {exc.reason}") from exc
            specs[name] = {
                "selector": dict(svc.spec.selector or {}),
                "ports": sorted((p.port, str(p.target_port), p.protocol) for p in svc.spec.ports or []),
                "session_affinity": svc.spec.session_affinity,
            }
        try:
            deployment = self.kubectl.apps_v1_api.read_namespaced_deployment(self.RECOMMENDATION, self.namespace)
        except ApiException as exc:
            raise RuntimeError(f"could not read deployment/{self.RECOMMENDATION}: {exc.status} {exc.reason}") from exc
        pod_spec = deployment.spec.template.spec
        specs["recommendation-dns"] = ApiClient().sanitize_for_serialization(
            {
                "host_aliases": pod_spec.host_aliases,
                "dns_policy": pod_spec.dns_policy,
                "dns_config": pod_spec.dns_config,
            }
        )
        return specs

    def upstream_addrs(self) -> list[str]:
        """
        ip:port of every running product-catalog pod (the pods the delay selects).
        """
        try:
            pods = self.kubectl.core_v1_api.list_namespaced_pod(
                self.namespace, label_selector=f"{self.SELECTOR_LABEL}={self.PRODUCT_CATALOG}"
            ).items
        except ApiException as exc:
            raise RuntimeError(f"could not list product-catalog pods: {exc.status} {exc.reason}") from exc
        return [
            f"{pod.status.pod_ip}:{self.UPSTREAM_PORT}"
            for pod in pods
            if pod.metadata.deletion_timestamp is None and pod.status.phase == "Running" and pod.status.pod_ip
        ]

    def release_client(self) -> None:
        """
        Delete the client pod, if one is running.
        """
        if not self._client:
            return
        name, self._client = self._client, None
        self.kubectl.exec_command_checked(
            f"kubectl delete pod {name} -n {self.namespace} --ignore-not-found --wait=true --timeout=60s", timeout=90
        )

    def catalog_ids(self) -> list[str]:
        out = self._psql("SELECT id FROM catalog.products ORDER BY id")
        return [line.strip() for line in out.splitlines() if line.strip()]

    def catalog_rows(self) -> list[str]:
        """
        Every product row as one line, to tell whether the original catalog survived.
        """
        out = self._psql(
            "SELECT id, name, description, picture, price_currency_code, price_units, price_nanos, categories "
            "FROM catalog.products ORDER BY id"
        )
        return [line for line in out.splitlines() if line.strip()]

    def new_product_id(self) -> str:
        existing = set(self.catalog_ids())
        while True:
            product_id = "".join(random.choices(string.ascii_uppercase + string.digits, k=10))
            if product_id not in existing:
                return product_id

    def insert_probe_product(self, product_id: str | None = None) -> str:
        """
        Insert a new product the way the catalog would grow, and return its id
        """
        product_id = product_id or self.new_product_id()
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

    # Helpers

    @staticmethod
    def _incident_confirmed(results: list[dict]) -> bool:
        """
        Every call answered, and every answer is the upstream deadline error the buggy fetch raises.
        """
        return bool(results) and all(
            not r["ok"] and r.get("code") == "UNKNOWN" and "DEADLINE_EXCEEDED" in (r.get("error") or "")
            for r in results
        )

    @classmethod
    def _parse_delay(cls, delay: str) -> float:
        if not re.fullmatch(r"\d+(\.\d+)?s", delay) or float(delay[:-1]) < cls.MIN_DELAY_S:
            raise ValueError(f"delay must look like '3s' and be at least {cls.MIN_DELAY_S:g}s, got {delay!r}")
        return float(delay[:-1])

    def _deployment_image(self, name: str) -> str:
        try:
            deployment = self.kubectl.apps_v1_api.read_namespaced_deployment(name, self.namespace)
        except ApiException as exc:
            raise RuntimeError(f"could not read deployment/{name}: {exc.status} {exc.reason}") from exc
        containers = deployment.spec.template.spec.containers
        return next((c.image for c in containers if c.name == name), containers[0].image)

    def _client_pod(self) -> str:
        """
        Start (once) a pod from the stock recommendation image to send probe calls from.
        """
        if self._client:
            return self._client
        image = self._client_image or self._deployment_image(self.RECOMMENDATION)
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
        name = f"{self.RECOMMENDATION}-client-{suffix}"
        spec = {
            "restartPolicy": "Never",
            "terminationGracePeriodSeconds": 0,
            "containers": [
                {"name": self.CLIENT_CONTAINER, "image": image, "command": ["sleep", "3600"], "workingDir": "/app"}
            ],
        }
        rollout = self._rollout_pod_objects()
        if rollout:
            # Same node as recommendation, so the image is already there.
            spec["nodeName"] = rollout[-1].spec.node_name
        manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "namespace": self.namespace},
            "spec": spec,
        }
        self.kubectl.exec_command_checked("kubectl apply -f -", input_data=json.dumps(manifest), timeout=60)
        self._client = name
        self.kubectl.exec_command_checked(
            f"kubectl wait pod/{name} -n {self.namespace} --for=condition=Ready --timeout=180s", timeout=200
        )
        return name

    def _warm_cache(self, timeout_s: float = 120) -> None:
        """
        Require WARMUP_CALLS successful answers in a row. product-catalog can still be restarting right after deploy.
        """
        deadline = time.monotonic() + timeout_s
        streak, last = 0, None
        while time.monotonic() < deadline:
            last = self.probe_recommendations(1, 0, 5.0)[0]
            streak = streak + 1 if last["ok"] else 0
            if streak >= self.WARMUP_CALLS:
                return
            time.sleep(0.5 if last["ok"] else 3)
        raise RuntimeError(f"recommendation failed while warming its cache: {last}")

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
        Newest running pod of the Deployment's current ReplicaSet.
        """
        pods = self._rollout_pod_objects()
        if not pods:
            raise RuntimeError("No running pod in the current recommendation rollout")
        return pods[-1].metadata.name

    def _rollout_pod_objects(self) -> list:
        """
        Running, non-terminating pods owned by the Deployment's current ReplicaSet, oldest first.

        A label match is not enough: a pod started by hand can carry the same labels without belonging to the
        rollout, and `kubectl exec deploy/recommendation` can land on a pod still terminating after a rollout.
        """
        apps, core = self.kubectl.apps_v1_api, self.kubectl.core_v1_api
        try:
            deployment = apps.read_namespaced_deployment(self.RECOMMENDATION, self.namespace)
            labels = deployment.spec.selector.match_labels or {}
            selector = ",".join(f"{k}={v}" for k, v in labels.items())
            replica_sets = apps.list_namespaced_replica_set(self.namespace, label_selector=selector).items
            pods = core.list_namespaced_pod(self.namespace, label_selector=selector).items
        except ApiException as exc:
            raise RuntimeError(f"could not look up the recommendation rollout: {exc.status} {exc.reason}") from exc

        revision = (deployment.metadata.annotations or {}).get("deployment.kubernetes.io/revision")
        current = {
            rs.metadata.uid
            for rs in replica_sets
            if any(o.uid == deployment.metadata.uid for o in rs.metadata.owner_references or [])
            and (rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision") == revision
        }
        live = [
            p
            for p in pods
            if p.metadata.deletion_timestamp is None
            and p.status.phase == "Running"
            and any(o.uid in current for o in p.metadata.owner_references or [])
        ]
        return sorted(live, key=lambda p: p.metadata.creation_timestamp)

    def _exec_in_rollout(self, script: str, args: list[str], timeout: float, pod: str | None = None) -> str:
        # -I ignores PYTHONPATH and the other PYTHON* variables set on the container.
        pod = pod or self._recommendation_pod()
        argv = " ".join(shlex.quote(a) for a in args)
        command = f"kubectl exec -i -n {self.namespace} {pod} -c {self.RECOMMENDATION} -- /venv/bin/python -I - {argv}"
        return self.kubectl.exec_command_checked(command, input_data=script, timeout=timeout)

    def _exec_in_client(self, script: str, args: list[str], timeout: float) -> str:
        pod = self._client_pod()
        argv = " ".join(shlex.quote(a) for a in args)
        command = f"kubectl exec -i -n {self.namespace} {pod} -c {self.CLIENT_CONTAINER} -- /venv/bin/python - {argv}"
        return self.kubectl.exec_command_checked(command, input_data=script, timeout=timeout)

    def _psql(self, query: str) -> str:
        command = (
            f"kubectl exec -n {self.namespace} deploy/{self.POSTGRES_DEPLOY} -- "
            f"env PGPASSWORD={self.PG_PASSWORD} psql -U {self.PG_SUPERUSER} -d {self.PG_DB} "
            f"-v ON_ERROR_STOP=1 -tA -c {shlex.quote(query)}"
        )
        return self.kubectl.exec_command_checked(command, timeout=60)

    def _ensure_chaos_mesh(self) -> None:
        # Same installer SREGym's noise injection uses. It waits for the controller and daemon rollouts
        # and sets _chaos_mesh_ready only when both finished.
        manager = get_noise_manager()
        manager._ensure_chaos_mesh_installed()
        if not manager._chaos_mesh_ready:
            raise RuntimeError("Chaos Mesh is not installed or not ready")
        self.kubectl.exec_command_checked(
            "kubectl get crd networkchaos.chaos-mesh.org schedules.chaos-mesh.org", timeout=30
        )

    def _chaos_crds_installed(self) -> bool:
        out = self.kubectl.exec_command_checked(
            "kubectl get crd schedules.chaos-mesh.org networkchaos.chaos-mesh.org -o name --ignore-not-found",
            timeout=30,
        )
        return len(out.split()) == 2

    def _delete_chaos(self) -> None:
        """Stop the Schedule and let Chaos Mesh recover every generated delay before deletion completes."""
        if not self._chaos_crds_installed():
            return
        deadline = time.monotonic() + 60
        left = self._chaos_objects()
        while left:
            for name in left:
                if not name.startswith("schedule.chaos-mesh.org/"):
                    continue
                try:
                    self.kubectl.exec_command_checked(
                        f"kubectl annotate {name} -n {self.namespace} experiment.chaos-mesh.org/pause=true --overwrite",
                        timeout=30,
                    )
                except RuntimeError as exc:
                    if "NotFound" not in str(exc):
                        raise
            # Delete children explicitly, independent of garbage collection of the Schedule. Children may outlive
            # a previously deleted Schedule or appear during its last reconciliation. Keep their recovery finalizers.
            self.kubectl.exec_command_checked(
                f"kubectl delete {' '.join(left)} -n {self.namespace} "
                "--ignore-not-found --cascade=background --wait=false",
                timeout=30,
            )
            if time.monotonic() >= deadline:
                raise RuntimeError(f"chaos objects named {self.CHAOS_NAME}* still present after 60s: {left}")
            time.sleep(2)
            left = self._chaos_objects()

    def _chaos_objects(self) -> list[str]:
        out = self.kubectl.exec_command_checked(
            f"kubectl get {self.CHAOS_RESOURCES} -n {self.namespace} -o name", timeout=30
        )
        return [
            line
            for line in out.split()
            if line.partition("/")[2] == self.CHAOS_NAME or line.partition("/")[2].startswith(f"{self.CHAOS_NAME}-")
        ]

    def _namespace_exists(self) -> bool:
        out = self.kubectl.exec_command_checked(
            f"kubectl get namespace {self.namespace} -o name --ignore-not-found", timeout=30
        )
        return bool(out.strip())

    def _cleanup_after_failed_injection(self) -> None:
        for step in (self._delete_chaos, self._remove_overlay, self.release_client):
            try:
                step()
            except Exception as exc:
                print(f"[Cleanup] {step.__name__} failed: {exc}")
