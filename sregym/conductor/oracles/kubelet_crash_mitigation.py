"""Grade shop recovery without requiring the agent to restart node daemons."""

import hashlib
import json
import subprocess
import time
import uuid
from collections import Counter
from urllib.parse import parse_qsl, urlsplit

from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle
from sregym.service.agent_visibility_policy import HIDDEN_LABELS
from sregym.service.rollout import deployment_rollout_complete


class KubeletCrashMitigationOracle(Oracle):
    """Require current serving workloads, preserved records and live orders.

    Node and retired-pod alerts can remain after a safe application failover.
    They are not a substitute for checking the application's actual recovery.
    """

    importance = 1.0
    observation_seconds = 60
    rollout_seconds = 60
    order_timeout_seconds = 60
    poll_seconds = 5
    workload_poll_seconds = 2
    product_id = "OLJCESPC7Z"
    tables = ('accounting."order"', "accounting.orderitem", "accounting.shipping", "reviews.productreviews")

    def __init__(self, problem):
        super().__init__(problem)
        self.deployments = set()
        self.services = {}
        self.records = None
        self.cart_user = uuid.uuid4().hex
        self.workload_pods = {}

    def _request(self, resource, path, payload=None):
        """The API proxy needs no exec connection to a failed worker's kubelet."""
        route = urlsplit(path)
        try:
            body, status, _ = self.problem.kubectl.core_v1_api.api_client.call_api(
                f"/api/v1/namespaces/{self.problem.namespace}/{resource}/proxy{route.path}",
                "GET" if payload is None else "POST",
                query_params=parse_qsl(route.query),
                header_params={"Accept": "application/json", "Content-Type": "application/json"},
                body=payload,
                response_type="object",
                auth_settings=["BearerToken"],
                _return_http_data_only=False,
                _request_timeout=(5, 15),
            )
        except ApiException as exc:
            # A proxy error may come from the application, not the API server.
            # It is failed evidence, not proof of an infrastructure outage.
            raise ValueError(f"Could not verify {resource}: HTTP {exc.status}") from exc
        if status != 200:
            raise ValueError(f"Application returned HTTP {status}")
        return body

    def _shop(self, path, payload=None):
        return self._request("services/frontend-proxy:8080", path, payload)

    def _topology(self):
        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        deployments = {dep.metadata.name: dep for dep in kubectl.list_deployments(namespace).items}
        owners = {
            rs.metadata.uid: owner.name
            for rs in kubectl.apps_v1_api.list_namespaced_replica_set(namespace).items
            for owner in rs.metadata.owner_references or []
            if owner.kind == "Deployment"
            and owner.name in deployments
            and owner.uid == deployments[owner.name].metadata.uid
            and (rs.spec.replicas or 0) > 0
        }
        pods = {pod.metadata.uid: pod for pod in kubectl.list_pods(namespace).items}
        pod_owners = {
            uid: owners[owner.uid]
            for uid, pod in pods.items()
            for owner in pod.metadata.owner_references or []
            if owner.kind == "ReplicaSet" and owner.uid in owners
        }
        endpoints = {
            ep.metadata.name: [address for subset in ep.subsets or [] for address in subset.addresses or []]
            for ep in kubectl.core_v1_api.list_namespaced_endpoints(namespace).items
        }
        return deployments, pods, pod_owners, endpoints

    def _serving_failure(self):
        deployments, pods, owners, endpoints = self._topology()
        ready_nodes = {
            node.metadata.name
            for node in self.problem.kubectl.list_nodes().items
            if any(c.type == "Ready" and c.status == "True" for c in node.status.conditions or [])
        }
        for name in sorted(self.deployments):
            dep = deployments.get(name)
            if dep is None:
                return self.fail("required_deployment_missing", deployment=name)
            if dep.spec.replicas == 0:
                return self.fail("required_deployment_scaled_to_zero", deployment=name)
            if not deployment_rollout_complete(dep):
                return self.fail("deployment_replicas_unready", deployment=name)
            active = [
                pod for uid, pod in pods.items() if owners.get(uid) == name and not pod.metadata.deletion_timestamp
            ]
            if not active or self.pods_unready(active) or any(pod.spec.node_name not in ready_nodes for pod in active):
                return self.fail("active_application_pods_unready", deployment=name)
        for name, expected in self.services.items():
            addresses = endpoints.get(name, [])
            if not addresses:
                return self.fail("no_ready_endpoints", service=name)
            for address in addresses:
                target = address.target_ref
                pod = pods.get(target.uid) if target is not None and target.kind == "Pod" else None
                if (
                    pod is None
                    or pod.metadata.deletion_timestamp
                    or owners.get(target.uid) not in expected
                    or pod.spec.node_name not in ready_nodes
                    or self.pods_unready([pod])
                ):
                    return self.fail("unexpected_or_unready_endpoint", service=name)
        return None

    def _sql(self, query):
        # Resolve the current Service endpoint, not the oldest pod matching a
        # label. A stranded old database may still be listed as Running.
        core = self.problem.kubectl.core_v1_api
        endpoints = core.read_namespaced_endpoints("postgresql", self.problem.namespace)
        addresses = [address for subset in endpoints.subsets or [] for address in subset.addresses or []]
        if not addresses or addresses[0].target_ref is None:
            raise ValueError("PostgreSQL has no pod endpoint")
        target = addresses[0].target_ref
        pod = core.read_namespaced_pod(target.name, self.problem.namespace)
        if target.kind != "Pod" or pod.metadata.uid != target.uid:
            raise ValueError("PostgreSQL endpoint identity changed")
        result = subprocess.run(
            [
                "kubectl",
                "-n",
                self.problem.namespace,
                "exec",
                pod.metadata.name,
                "--",
                "psql",
                "-U",
                "root",
                "-d",
                "otel",
                "-v",
                "ON_ERROR_STOP=1",
                "-Atc",
                query,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        return result.stdout.strip()

    def _records(self):
        snapshots = {}
        for table in self.tables:
            rows = json.loads(self._sql(f"SELECT coalesce(json_agg(t), '[]') FROM {table} t"))
            snapshots[table] = Counter(
                hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest() for row in rows
            )
        return snapshots

    def _cart(self, user):
        return self._shop(f"/api/cart?currencyCode=USD&sessionId={user}")

    def _add_item(self, user):
        self._shop("/api/cart", {"userId": user, "item": {"productId": self.product_id, "quantity": 2}})

    def _checkout(self):
        previous = set(json.loads(self._sql("SELECT coalesce(json_agg(order_id), '[]') FROM accounting.\"order\"")))
        user = uuid.uuid4().hex
        self._add_item(user)
        result = self._shop(
            "/api/checkout?currencyCode=USD",
            {
                "userId": user,
                "email": "customer@example.com",
                "address": {
                    "streetAddress": "1 Main Street",
                    "zipCode": "61801",
                    "city": "Urbana",
                    "state": "IL",
                    "country": "United States",
                },
                "userCurrency": "USD",
                "creditCard": {
                    "creditCardNumber": "4432-8015-6152-0454",
                    "creditCardExpirationMonth": 1,
                    "creditCardExpirationYear": 2039,
                    "creditCardCvv": 672,
                },
            },
        )
        order_id = str(uuid.UUID(result["orderId"]))
        if order_id in previous:
            raise ValueError("Checkout returned an existing order")
        if result["items"][0]["item"]["productId"] != self.product_id or result["items"][0]["item"]["quantity"] != 2:
            raise ValueError("Checkout did not preserve the requested cart")
        deadline = time.monotonic() + self.order_timeout_seconds
        query = (
            'SELECT count(*) FROM accounting."order" o JOIN accounting.orderitem i USING (order_id) '
            "JOIN accounting.shipping s USING (order_id) "
            f"WHERE o.order_id='{order_id}' AND i.product_id='{self.product_id}' AND i.quantity=2"
        )
        while self._sql(query) != "1":
            if time.monotonic() >= deadline:
                raise ValueError("Accepted order did not reach accounting")
            time.sleep(self.poll_seconds)
        return order_id

    def _workload(self):
        totals = Counter()
        for name, uid in self.workload_pods.items():
            pod = self.problem.kubectl.core_v1_api.read_namespaced_pod(name, self.problem.namespace)
            if pod.metadata.uid != uid:
                raise ValueError("The original workload was replaced")
            state = self._request(f"pods/{name}:8089", "/stats/requests")
            if state["state"] != "running" or state["user_count"] < 1:
                raise ValueError("The workload is not sending traffic")
            aggregate = next(row for row in state["stats"] if row["name"] == "Aggregated")
            totals.update(requests=aggregate["num_requests"], failures=aggregate["num_failures"])
        return totals

    def _observe_workload(self):
        # The normal fetcher resets Locust every ten seconds, but can lose its
        # route after the node failure. Poll the same original Locust processes
        # in both cases. Discard intervals whose counters cross a reset.
        previous = self._workload()
        totals = Counter()
        intervals = resets = 0
        deadline = time.monotonic() + self.observation_seconds
        while time.monotonic() < deadline:
            time.sleep(self.workload_poll_seconds)
            current = self._workload()
            delta = {key: current[key] - previous[key] for key in ("requests", "failures")}
            if any(value < 0 for value in delta.values()):
                resets += 1
            else:
                totals.update(delta)
                intervals += 1
            previous = current
        print(f"Observed workload: {dict(totals)}, intervals={intervals}, resets={resets}", flush=True)
        return totals, intervals

    def capture_baseline(self):
        deployments, pods, owners, endpoints = self._topology()
        self.deployments = {
            name
            for name, dep in deployments.items()
            if not any((dep.metadata.labels or {}).get(key) in values for key, values in HIDDEN_LABELS.items())
        }
        self.services = {
            name: expected
            for name, addresses in endpoints.items()
            if (expected := {owners.get(address.target_ref.uid) for address in addresses if address.target_ref})
            and expected <= self.deployments
        }
        self.workload_pods = {
            pod.metadata.name: uid
            for uid, pod in pods.items()
            if owners.get(uid) == "load-generator" and pod.metadata.deletion_timestamp is None
        }
        if not self.deployments or not self.workload_pods or self._serving_failure():
            raise RuntimeError("Cannot capture an unhealthy shop baseline")
        self._workload()
        self._add_item(self.cart_user)
        self.cart = self._cart(self.cart_user)
        if not any(item["productId"] == self.product_id and item["quantity"] == 2 for item in self.cart["items"]):
            raise ValueError("The saved cart did not retain its items")
        self._checkout()
        self.records = self._records()

    def evaluate(self):
        print("== Kubelet crash application recovery evaluation ==", flush=True)
        if self.records is None:
            return self.fail("oracle_baseline_missing")
        try:
            deadline = time.monotonic() + self.rollout_seconds
            failure = self._serving_failure()
            while failure and time.monotonic() < deadline:
                time.sleep(self.poll_seconds)
                failure = self._serving_failure()
            if failure:
                return failure
            failure = self._data_failure()
            if failure:
                return failure
            if self._cart(self.cart_user) != self.cart:
                return self.fail("application_cart_changed")
            product = self._shop(f"/api/products/{self.product_id}?currencyCode=EUR")
            if product["id"] != self.product_id or product["priceUsd"]["currencyCode"] != "EUR":
                return self.fail("application_response_invalid")
            orders = [self._checkout()]
            totals, intervals = self._observe_workload()
            orders.append(self._checkout())
            requests, failures = totals["requests"], totals["failures"]
            if intervals < 3 or requests < 20 or failures < 0 or failures > requests * 0.01:
                return self.fail("application_workload_unhealthy", requests=requests, failures=failures)
            failure = self._serving_failure()
            if failure:
                return failure
            failure = self._data_failure()
            if failure:
                return failure
            result = {"success": True, "detail": {"new_orders": orders, "requests": requests, "failures": failures}}
            print(f"Application recovery verified: {result}", flush=True)
            return result
        except (ValueError, KeyError, TypeError, StopIteration) as exc:
            return self.fail("application_recovery_unverified", error=str(exc))
        except Exception as exc:
            return self.fail_from_exception(exc)

    def _data_failure(self):
        current = self._records()
        missing = {table: sum((rows - current[table]).values()) for table, rows in self.records.items()}
        if any(missing.values()):
            return self.fail("application_data_changed", missing_or_changed=missing)
        return None
