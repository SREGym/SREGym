"""A retained edge resolver splits acknowledged cart histories at store cutover."""

import contextlib
import copy
import json
import random
import shlex
import threading
import time
import uuid
from pathlib import Path

import yaml
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.stale_hostaliases_mitigation import (
    CartRequestError,
    ServingCapacityError,
    StaleHostAliasesMitigationOracle,
    cart_operations,
)
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.observer.jaeger import Jaeger
from sregym.observer.otel_collector import OtelCollector
from sregym.profile import is_svelte
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.helm import Helm
from sregym.service.kubectl import KubeCtl

CART_CLIENT = r"""
import json,sys,time,urllib.request,urllib.parse,urllib.error
out=[]
for action in json.loads(sys.argv[1]):
    op=action['op']; user=action['user']; url=action['url']+'/api/cart'
    body=None; headers={'Content-Type':'application/json',
        'traceparent':'00-'+action['trace']+'-'+action['trace'][:16]+'-01'}
    if op=='read':
        url+='?'+urllib.parse.urlencode({'sessionId':user,'currencyCode':'USD'})
        method='GET'
    else:
        method='DELETE' if op=='clear' else 'POST'
        payload={'userId':user}
        if op=='add':
            payload['item']={'productId':action['product'],'quantity':action['quantity']}
            url+='?currencyCode=USD'
        body=json.dumps(payload).encode()
    request=urllib.request.Request(url,data=body,headers=headers,method=method)
    try:
        with urllib.request.urlopen(request,timeout=20) as response:
            raw=response.read(); data=json.loads(raw) if raw.strip() else {}
    except (urllib.error.URLError,TimeoutError,json.JSONDecodeError) as error:
        print(json.dumps({'request_error':str(error),'method':method,'url':url}))
        sys.exit(0)
    receipt=dict(action,finished_us=time.time_ns()//1000)
    if op=='read':
        items=data.get('items',[]) if isinstance(data,dict) else data
        receipt['cart']={item['productId']:item['quantity'] for item in items}
    out.append(receipt)
print(json.dumps(out))
"""


class StaleHostAliasesDNSPoisoningAstronomyShop(Problem):
    """Preserve customer intent across two healthy, inconsistently routed stores."""

    run_default_workload = False
    FAULTY_SERVICE = "frontend-proxy"
    TARGET_BACKEND = "frontend"
    BLACKHOLE_IP = "127.0.0.1"  # Original injector's public default; this variant pins a live release.
    PRODUCTS = ("OLJCESPC7Z", "2ZYFJ3GM2N", "66VCHSJNUP")
    EDGES = ("frontend-proxy",)
    EDGE_REPLICAS = 2
    RETIRED_ROUTES = ("frontend-retained", "cart-retained")
    OPERATIONS = "storefront-operations"

    def __init__(self):
        super().__init__(AstronomyShop())
        self.kubectl = KubeCtl()
        self.faulty_service, self.target_backend = self.FAULTY_SERVICE, self.TARGET_BACKEND
        self.blackhole_ip = self.BLACKHOLE_IP
        self.injector = VirtualizationFaultInjector(namespace=self.namespace)
        self.expected_carts, self.events = {}, []
        self.unacknowledged = []
        self._lock, self._stop = threading.Lock(), threading.Event()
        self._thread, self._prepared, self.traffic_error = None, False, ""
        self.started_us = time.time_ns() // 1000
        self._original_cleanup = getattr(self.app, "cleanup", None)
        self.app.deploy, self.app.cleanup = self.deploy_app, self.cleanup
        self.app.description = getattr(self.app, "description", "") + (
            " Customers report cart items disappearing or reappearing between visits after a release cutover. "
            "Restore consistent carts while preserving every acknowledged add and clear, including earlier "
            "customer state. Example sessions are in ConfigMap customer-support; normal application logs "
            "and traces retain customer activity. Availability alone is insufficient. Maintenance may pause "
            "cart traffic by setting acceptWrites=false in ConfigMap storefront-operations; wait for status=paused "
            "before reconciling data, then resume service. Preserve at least two serving frontend-proxy replicas."
            " Unacknowledged requests, if any, are listed by trace ID in that operational record."
            " The frontend-retained and cart-retained Service entrypoints are scheduled for retirement."
            " Public cart operations must remain correct when those entrypoints are withdrawn and callers"
            " are replaced. Either database may remain authoritative; do not discard acknowledged customer state."
        )
        self.root_cause = self.build_structured_root_cause(
            component="deployment/frontend-proxy",
            namespace=self.namespace,
            description=(
                "One old frontend-proxy Pod still uses FRONTEND_HOST=frontend and a hostAliases mapping "
                "to frontend-retained. Its sibling uses the intended current-frontend FQDN without an alias. "
                "Both are Ready behind the same Service and Deployment: a paused partial rollout retained "
                "the old ReplicaSet despite the corrected desired template. At Valkey promotion the current frontend/cart "
                "route moved to the new store; retained frontend/cart still use the original store. Both "
                "routes successfully acknowledge cart mutations against independent copies. The root cause "
                "is the stale Pod-local resolver path surviving the paused release transition, not failed DNS "
                "or a broken store. Diagnose desired-versus-running configuration and trace that resolver "
                "path to the two divergent histories. Recovery requires persistent routing convergence and "
                "ordered reconstruction of successful adds and clears, not copying or unioning snapshots. "
                "The repaired public path must remain independent of the retiring frontend/cart entrypoints."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.hosts_mitigation_oracle = StaleHostAliasesMitigationOracle(problem=self)
        self.mitigation_oracle = self.hosts_mitigation_oracle
        self.app.create_workload()

    def _run(self, command, input_data=None, timeout=120):
        return self.kubectl.exec_command_checked(command, input_data=input_data, timeout=timeout)

    def _get(self, kind, name):
        return json.loads(self._run(f"kubectl -n {self.namespace} get {kind} {name} -o json"))

    def _apply(self, obj):
        return self._run(f"kubectl -n {self.namespace} apply -f -", json.dumps(obj))

    def _patch(self, kind, name, patch):
        return self._run(
            f"kubectl -n {self.namespace} patch {kind}/{name} --type=merge -p {shlex.quote(json.dumps(patch))}"
        )

    def _rollout(self, name):
        self._run(f"kubectl -n {self.namespace} rollout status deployment/{name} --timeout=180s", timeout=190)

    def _service(self, name, selector, port=8080):
        self._apply(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": name},
                "spec": {"selector": selector, "ports": [{"port": port, "targetPort": port}]},
            }
        )

    def _select_store(self, selector, service="valkey-cart"):
        patch = [{"op": "replace", "path": "/spec/selector", "value": selector}]
        self._run(
            f"kubectl -n {self.namespace} patch service/{service} --type=json -p {shlex.quote(json.dumps(patch))}"
        )

    def _clone(self, source, name, env=None, labels=None):
        dep = self._get("deployment", source)
        template = copy.deepcopy(dep["spec"]["template"])
        # A cloned database must never start against its source's AOF volume.
        for volume in template["spec"].get("volumes", []):
            claim = volume.get("persistentVolumeClaim")
            if claim and claim["claimName"] == source:
                claim["claimName"] = name
        selector = {"opentelemetry.io/name": name}
        template["metadata"]["labels"] = {**template["metadata"]["labels"], **selector, **(labels or {})}
        for container in template["spec"]["containers"]:
            for entry in container.get("env", []):
                if entry["name"] in (env or {}):
                    entry.pop("valueFrom", None)
                    entry["value"] = env[entry["name"]]
        self._apply(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": name},
                "spec": {"replicas": 1, "selector": {"matchLabels": selector}, "template": template},
            }
        )
        return selector

    def _ensure_trace_backend(self):
        # Direct lifecycle tests need observers, but the normal Conductor has
        # already deployed them. Recreating their Services can interrupt export.
        for name, observer in (("jaeger-agent", Jaeger()), ("otel-collector", OtelCollector())):
            try:
                self.kubectl.get_deployment(name, "observe")
            except ApiException as exc:
                if exc.status != 404:
                    raise
                observer.deploy()
            self._run(f"kubectl -n observe rollout status deployment/{name} --timeout=120s", timeout=130)

    def _wait_trace_collector(self):
        self._run(
            f"kubectl -n {self.namespace} rollout status daemonset/otel-collector-agent --timeout=120s",
            timeout=130,
        )

    def deploy_app(self):
        # Scope baseline options to this app instance; no shared app or image changes.
        self._ensure_trace_backend()
        chart = Path(self.app.helm_configs["chart_path"])
        sidecars = yaml.safe_load((chart / "values.yaml").read_text())["components"]["flagd"]["sidecarContainers"]
        sidecars[0].setdefault("envOverrides", []).append({"name": "ERL_FLAGS", "value": "+Q 65536"})
        args = ["--set", "prometheus.enabled=false", "-f", str(AstronomyShop._VALUES_DIR / "astronomy-shop-fixes.yaml")]
        if is_svelte():
            args += ["-f", str(AstronomyShop._VALUES_DIR / "astronomy-shop-svelte.yaml")]
        settings = {
            "components.flagd.sidecarContainers": sidecars,
            "components.load-generator.envOverrides": [
                {"name": "LOCUST_BROWSER_TRAFFIC_ENABLED", "value": "false"},
                {"name": "LOCUST_AUTOSTART", "value": "false"},
            ],
            "components.product-catalog.resources.limits.memory": "128Mi",
            "components.fraud-detection.resources.limits.memory": "512Mi",
            "components.valkey-cart.resources.limits.memory": "64Mi",
        }
        for key, value in settings.items():
            args += ["--set-json", shlex.quote(f"{key}={json.dumps(value)}")]
        Helm.install(**{**self.app.helm_configs, "extra_args": args})
        Helm.assert_if_deployed(self.namespace)
        Jaeger().create_external_name_service(self.namespace)
        self._wait_trace_collector()
        self.prepare_baseline()

    def _persist_store(self, name):
        self._apply(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {"name": name},
                "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "64Mi"}}},
            }
        )
        template = self._get("deployment", name)["spec"]["template"]
        spec = template["spec"]
        spec.setdefault("securityContext", {})["fsGroup"] = 1000
        spec["volumes"] = [{"name": "data", "persistentVolumeClaim": {"claimName": name}}]
        container = spec["containers"][0]
        container["command"] = ["valkey-server", "--appendonly", "yes", "--appendfsync", "always", "--dir", "/data"]
        container["volumeMounts"] = [{"name": "data", "mountPath": "/data"}]
        self._patch(
            "deployment",
            name,
            {"spec": {"strategy": {"type": "Recreate", "rollingUpdate": None}, "template": template}},
        )
        self._rollout(name)

    def _redis(self, name, *args):
        return self._run(
            f"kubectl -n {self.namespace} exec deployment/{name} -- valkey-cli --raw {shlex.join(args)}"
        ).strip()

    def _replication(self, enabled):
        template = self._get("deployment", "valkey-next")["spec"]["template"]
        container = template["spec"]["containers"][0]
        command = container["command"]
        if "--replicaof" in command:
            command = command[: command.index("--replicaof")]
        container["command"] = command + (["--replicaof", "valkey-retained", "6379"] if enabled else [])
        self._patch("deployment", "valkey-next", {"spec": {"template": template}})
        self._rollout("valkey-next")

    def client(self, actions, record=False):
        actions = [{"trace": uuid.uuid4().hex, **action} for action in actions]
        output = self._run(
            f"kubectl -n {self.namespace} exec -i deployment/load-generator -- python - {shlex.quote(json.dumps(actions))}",
            CART_CLIENT,
            timeout=max(60, len(actions) * 25),
        )
        receipts = json.loads(output)
        if isinstance(receipts, dict) and "request_error" in receipts:
            raise CartRequestError(f"{receipts['method']} {receipts['url']}: {receipts['request_error']}")
        if len(receipts) != len(actions):
            raise RuntimeError("cart request batch did not return every acknowledgement")
        if record:
            with self._lock:
                for item in receipts:
                    cart = self.expected_carts.setdefault(item["user"], {})
                    if item["op"] == "clear":
                        cart.clear()
                    elif item["op"] == "add":
                        cart[item["product"]] = cart.get(item["product"], 0) + item["quantity"]
                    self.events.append(item)
        return receipts

    def _edge_pods(self):
        deployment = self._get("deployment", self.faulty_service)
        selector = deployment["spec"]["selector"]["matchLabels"]
        labels = shlex.quote(",".join(f"{key}={value}" for key, value in selector.items()))
        pods = [
            pod
            for pod in self._get("pods", f"-l {labels}")["items"]
            if not pod["metadata"].get("deletionTimestamp")
            and pod["status"].get("podIP")
            and any(c["type"] == "Ready" and c["status"] == "True" for c in pod["status"].get("conditions", []))
        ]
        if len(pods) < self.EDGE_REPLICAS:
            raise ServingCapacityError(
                f"edge '{self.faulty_service}' has fewer than {self.EDGE_REPLICAS} ready replicas"
            )
        return sorted(pods, key=lambda pod: pod["metadata"]["name"])

    def cohort_urls(self):
        # Both revisions have the same Service/Deployment identity. Target live
        # Pods for deterministic workload coverage, without extra cohort Services.
        return [f"http://{pod['status']['podIP']}:8080" for pod in self._edge_pods()]

    @contextlib.contextmanager
    def retired_routes_withdrawn(self):
        """Rehearse declared Service retirement without completing a pending rollout."""
        selectors = {}
        try:
            for name in self.RETIRED_ROUTES:
                raw = self._run(f"kubectl -n {self.namespace} get service {name} --ignore-not-found -o json")
                if not raw.strip():
                    continue  # An agent may already have removed an unused legacy entrypoint.
                selectors[name] = json.loads(raw)["spec"].get("selector", {})
                self._select_store({"retired-entrypoint": uuid.uuid4().hex}, service=name)
            deadline = time.monotonic() + 30
            while any(
                subset.get("addresses") or subset.get("notReadyAddresses")
                for name in selectors
                for subset in self._get("endpoints", name).get("subsets", [])
            ):
                if time.monotonic() >= deadline:
                    raise RuntimeError("legacy entrypoint withdrawal did not converge")
                time.sleep(1)
            yield
        finally:
            for name, selector in selectors.items():
                self._select_store(selector, service=name)

    def serving_urls(self):
        endpoints = self._get("endpoints", "frontend-proxy")
        addresses = {a["ip"] for s in endpoints.get("subsets", []) for a in s.get("addresses", [])}
        if not addresses:
            raise ServingCapacityError("storefront has no serving endpoints")
        ready = {pod["status"]["podIP"] for pod in self._edge_pods()}
        if not ready <= addresses:
            raise ServingCapacityError(f"edge '{self.faulty_service}' is not fully serving through the public Service")
        return ["http://frontend-proxy:8080", *(f"http://{ip}:8080" for ip in sorted(addresses))]

    def wait_reads(self):
        deadline = time.monotonic() + 90
        while True:
            try:
                self.client([{"op": "read", "user": "session-readiness", "url": url} for url in self.serving_urls()])
                return
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(2)

    def carts_match(self):
        with self._lock:
            expected = copy.deepcopy(self.expected_carts)
        actions = [{"op": "read", "user": user, "url": url} for url in self.serving_urls() for user in expected]
        receipts = self.client(actions)
        self.cart_mismatches = sorted({item["user"] for item in receipts if item["cart"] != expected[item["user"]]})
        return bool(expected) and not self.cart_mismatches

    def fresh_operations_work(self):
        urls, user, product = self.serving_urls(), "session-" + uuid.uuid4().hex, self.PRODUCTS[0]
        expected = {}
        for index, url in enumerate(urls):
            op = "clear" if index == len(urls) - 1 else "add"
            self.client([{"op": op, "user": user, "url": url, "product": product, "quantity": index + 2}])
            expected = {} if op == "clear" else {product: expected.get(product, 0) + index + 2}
            reads = self.client([{"op": "read", "user": user, "url": other} for other in urls])
            if any(item["cart"] != expected for item in reads):
                return False
        return True

    def prepare_baseline(self):
        self._persist_store("valkey-cart")
        self._clone("valkey-cart", "valkey-next")
        self._persist_store("valkey-next")
        original = self._get("service", "valkey-cart")["spec"]["selector"]
        self._service("valkey-retained", original, 6379)
        self._service("valkey-next", {"opentelemetry.io/name": "valkey-next"}, 6379)
        self._replication(True)
        self._service("cart-retained", self._clone("cart", "cart-retained", {"VALKEY_ADDR": "valkey-retained:6379"}))
        self._service(
            "frontend-retained", self._clone("frontend", "frontend-retained", {"CART_ADDR": "cart-retained:8080"})
        )
        self.blackhole_ip = self._get("service", "frontend-retained")["spec"]["clusterIP"]
        self._patch(
            "deployment",
            "frontend-proxy",
            {
                "spec": {
                    "replicas": self.EDGE_REPLICAS,
                    "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}},
                    "template": {
                        "spec": {"hostAliases": [{"ip": self.blackhole_ip, "hostnames": ["frontend"]}]},
                    },
                }
            },
        )
        for name in ("cart-retained", "frontend-retained", *self.EDGES):
            self._rollout(name)
        self.wait_reads()
        self._apply(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": self.OPERATIONS},
                "data": {"acceptWrites": "true", "status": "running", "unacknowledgedTraceIds": "[]"},
            }
        )
        actions, urls = [], self.cohort_urls()
        for index in range(16):
            user = "session-" + uuid.uuid4().hex
            actions.append({"op": "clear", "user": user, "url": urls[0]})
            for product in self.PRODUCTS[:2]:
                actions.append(
                    {
                        "op": "add",
                        "user": user,
                        "product": product,
                        "quantity": index % 3 + 1,
                        "url": urls[0],
                    }
                )
        self.client(actions, record=True)
        if not self.carts_match():
            raise RuntimeError("healthy baseline has inconsistent carts")
        # The Conductor restarts the app collector after deploy_app returns.
        # Require the history upstream before allowing that transition.
        self.assert_native_history()
        self._prepared = True

    def inject_fault(self):
        if not self._prepared or self.fault_injected:
            raise RuntimeError("a healthy, non-faulted baseline is required")
        self.stop_traffic()
        self._wait_trace_collector()
        # A recovered episode has no alias. Re-establish the healthy migration
        # route while both callers still use the same primary, before cutover.
        self._patch(
            "deployment",
            "frontend-proxy",
            {
                "spec": {
                    "paused": False,
                    "replicas": self.EDGE_REPLICAS,
                    "minReadySeconds": 0,
                    "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}},
                    "template": {"spec": {"hostAliases": [{"ip": self.blackhole_ip, "hostnames": ["frontend"]}]}},
                }
            },
        )
        self._run(f"kubectl -n {self.namespace} set env deployment/frontend-proxy FRONTEND_HOST=frontend")
        self._rollout("frontend-proxy")
        # Fence the last baseline write on this connection before promotion.
        output = self._run(
            f"kubectl -n {self.namespace} exec -i deployment/valkey-cart -- valkey-cli --raw",
            f"SET release-barrier {uuid.uuid4().hex}\nWAIT 1 15000\n",
        )
        if output.splitlines() != ["OK", "1"]:
            raise RuntimeError("candidate store did not acknowledge the cutover barrier")
        self._replication(False)
        self._select_store({"opentelemetry.io/name": "valkey-next"})
        self._run(f"kubectl -n {self.namespace} rollout restart deployment/cart")
        self._rollout("cart")
        self._pause_partial_edge_rollout()
        actions, urls = [], self.cohort_urls()
        for index, user in enumerate(list(self.expected_carts)[:12]):
            for step in range(6):
                actions.append(
                    {
                        "op": "clear" if step in (1, 4) and index % 3 != 0 else "add",
                        "user": user,
                        "product": self.PRODUCTS[(index + step) % 3],
                        "quantity": 1 + (index + step) % 4,
                        "url": urls[(index + step) % 2],
                    }
                )
        self.client(actions, record=True)
        if self.carts_match():
            raise RuntimeError("cutover did not produce divergent acknowledged cart histories")
        self.assert_native_history()
        self._apply(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "customer-support"},
                "data": {
                    "sessions.json": json.dumps(list(self.expected_carts)[:3]),
                    "report": "Items disappear or return between visits after the storefront release.",
                },
            }
        )
        self.fault_injected = True
        self.start_traffic()

    def _pause_partial_edge_rollout(self):
        template = copy.deepcopy(self._get("deployment", self.faulty_service)["spec"]["template"])
        current_host = f"frontend.{self.namespace}.svc.cluster.local"
        for container in template["spec"]["containers"]:
            for entry in container.get("env", []):
                if entry["name"] == "FRONTEND_HOST":
                    entry["value"] = current_host
        template["spec"]["hostAliases"] = None
        # Native rolling-update availability prevents the second replacement
        # while we pause after the first Ready Pod. Remove this setup-only delay
        # atomically with the pause; it is not a second fault left for the agent.
        self._patch("deployment", self.faulty_service, {"spec": {"minReadySeconds": 120, "template": template}})
        deadline = time.monotonic() + 90
        while True:
            try:
                pods = self._edge_pods()
            except ServingCapacityError:
                pods = []
            hosts = [
                entry.get("value")
                for pod in pods
                for container in pod["spec"]["containers"]
                for entry in container.get("env", [])
                if entry["name"] == "FRONTEND_HOST"
            ]
            if len(pods) == self.EDGE_REPLICAS and sorted(hosts) == sorted(["frontend", current_host]):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("edge rollout did not reach one old and one new Ready Pod")
            time.sleep(1)
        self._patch("deployment", self.faulty_service, {"spec": {"paused": True, "minReadySeconds": 0}})
        deadline = time.monotonic() + 30
        while True:
            deployment = self._get("deployment", self.faulty_service)
            status = deployment.get("status", {})
            if (
                status.get("observedGeneration", 0) >= deployment["metadata"]["generation"]
                and status.get("replicas") == self.EDGE_REPLICAS
                and status.get("availableReplicas") == self.EDGE_REPLICAS
                and status.get("updatedReplicas") == 1
            ):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("paused mixed edge did not become fully available")
            time.sleep(1)
        self.wait_reads()

    def assert_native_history(self):
        script = (
            "import json,sys,urllib.parse,urllib.request\n"
            "params=json.loads(sys.argv[1]); url='http://jaeger-out.observe.svc.cluster.local:16686/api/traces?'"
            "+urllib.parse.urlencode(params)\n"
            "with urllib.request.urlopen(url,timeout=20) as r: print(r.read().decode())\n"
        )
        deadline = time.monotonic() + 45
        expected = [
            (
                e["user"],
                e["op"],
                e.get("product") if e["op"] == "add" else None,
                e.get("quantity") if e["op"] == "add" else None,
            )
            for e in self.events
        ]
        while True:
            params = {"service": "cart", "start": self.started_us, "end": time.time_ns() // 1000, "limit": 2000}
            raw = self._run(
                f"kubectl -n {self.namespace} exec -i deployment/load-generator -- python - {shlex.quote(json.dumps(params))}",
                script,
            )
            response = json.loads(raw)
            if response.get("errors") or len(response["data"]) >= 2000:
                raise RuntimeError("native cart history query is incomplete")
            actual = cart_operations(response["data"], set(self.expected_carts), self.unacknowledged)
            if actual == expected:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"native cart history differs from acknowledgements: {len(actual)} versus {len(expected)} events"
                )
            time.sleep(2)

    def _traffic(self):
        rng = random.Random()
        while not self._stop.is_set():
            try:
                enabled = self._get("configmap", self.OPERATIONS)["data"].get("acceptWrites") == "true"
                self._patch("configmap", self.OPERATIONS, {"data": {"status": "running" if enabled else "paused"}})
                if enabled:
                    action = {
                        "op": "clear" if rng.randrange(6) == 0 else "add",
                        "user": rng.choice(list(self.expected_carts)),
                        "product": rng.choice(self.PRODUCTS),
                        "quantity": rng.randrange(1, 5),
                        "url": "http://frontend-proxy:8080",
                        "trace": uuid.uuid4().hex,
                    }
                    try:
                        self.client([action], record=True)
                    except RuntimeError:
                        # A lost response may hide a completed server mutation.
                        # Do not retry it or make recovery permanently impossible:
                        # retain its identity so native history can exclude it.
                        self.unacknowledged.append(action["trace"])
                        self._patch(
                            "configmap",
                            self.OPERATIONS,
                            {"data": {"unacknowledgedTraceIds": json.dumps(self.unacknowledged)}},
                        )
            except Exception as exc:
                self.traffic_error = str(exc)
                self._patch("configmap", self.OPERATIONS, {"data": {"status": "request-failed"}})
                return
            self._stop.wait(8)

    def start_traffic(self):
        self.stop_traffic()
        self._stop.clear()
        self.traffic_error = ""
        self._thread = threading.Thread(target=self._traffic, daemon=True)
        self._thread.start()

    def stop_traffic(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=90)
            if self._thread.is_alive():
                raise RuntimeError("cart request did not drain")
            self._thread = None

    def restart_callers(self):
        # Replace running Pods, including a paused edge's old ReplicaSet Pods.
        # Do not apply pending templates or otherwise finish an agent's repair.
        for name in ("cart", "cart-retained", "frontend", "frontend-retained", *self.EDGES):
            if (
                name in self.RETIRED_ROUTES
                and not self._run(
                    f"kubectl -n {self.namespace} get deployment {name} --ignore-not-found -o json"
                ).strip()
            ):
                continue
            deployment = self._get("deployment", name)
            if name in self.RETIRED_ROUTES and deployment["spec"].get("replicas", 1) == 0:
                continue
            selector = deployment["spec"]["selector"]["matchLabels"]
            labels = shlex.quote(",".join(f"{key}={value}" for key, value in selector.items()))
            old = self._get("pods", f"-l {labels}")["items"]
            old_ids = {pod["metadata"]["uid"] for pod in old}
            names = shlex.join(pod["metadata"]["name"] for pod in old)
            self._run(f"kubectl -n {self.namespace} delete pod {names} --wait=true --timeout=90s")
            deadline = time.monotonic() + 180
            while True:
                pods = self._get("pods", f"-l {labels}")["items"]
                if len(pods) == deployment["spec"].get("replicas", 1) and all(
                    pod["metadata"]["uid"] not in old_ids
                    and not pod["metadata"].get("deletionTimestamp")
                    and any(c["type"] == "Ready" and c["status"] == "True" for c in pod["status"].get("conditions", []))
                    for pod in pods
                ):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"replacement pods not ready: {name}")
                time.sleep(2)
        self.wait_reads()

    def recover_fault(self):
        self.stop_traffic()
        # Deployment cleanup also calls recovery when baseline setup failed.
        # No cutover is possible before preparation; app cleanup removes any
        # partially created resources without trying to reconstruct empty data.
        if not self._prepared:
            return
        restored = copy.deepcopy(self.expected_carts)
        self._redis("valkey-cart", "REPLICAOF", "NO", "ONE")
        # Retirement may have removed either unused legacy caller. Recreate
        # them for a healthy test baseline before restoring their Services.
        for source, name, env in (
            ("cart", "cart-retained", {"VALKEY_ADDR": "valkey-retained:6379"}),
            ("frontend", "frontend-retained", {"CART_ADDR": "cart-retained:8080"}),
        ):
            if not self._run(f"kubectl -n {self.namespace} get deployment {name} --ignore-not-found -o json").strip():
                self._clone(source, name, env)
            else:
                self._patch("deployment", name, {"spec": {"replicas": 1}})
        # Services are valid repair targets, not immutable baseline records.
        # Restore owned routes before replication, avoiding self-replication if
        # the agent pointed valkey-retained at the promoted candidate store.
        for service, deployment in (
            ("valkey-cart", "valkey-cart"),
            ("valkey-retained", "valkey-cart"),
            ("valkey-next", "valkey-next"),
            ("cart", "cart"),
            ("cart-retained", "cart-retained"),
            ("frontend", "frontend"),
            ("frontend-retained", "frontend-retained"),
        ):
            selector = self._get("deployment", deployment)["spec"]["selector"]["matchLabels"]
            if not self._run(f"kubectl -n {self.namespace} get service {service} --ignore-not-found -o json").strip():
                self._service(service, selector, 6379 if service.startswith("valkey-") else 8080)
                if service == "frontend-retained":
                    self.blackhole_ip = self._get("service", service)["spec"]["clusterIP"]
            else:
                self._select_store(selector, service=service)
        self._replication(True)
        for name, env in (
            ("cart", "VALKEY_ADDR=valkey-cart:6379"),
            ("cart-retained", "VALKEY_ADDR=valkey-retained:6379"),
            ("frontend", "CART_ADDR=cart:8080"),
            ("frontend-retained", "CART_ADDR=cart-retained:8080"),
        ):
            self._run(f"kubectl -n {self.namespace} set env deployment/{name} {env}")
            self._run(f"kubectl -n {self.namespace} rollout restart deployment/{name}")
            self._rollout(name)
        self._patch(
            "deployment",
            "frontend-proxy",
            {
                "spec": {
                    "paused": False,
                    "replicas": self.EDGE_REPLICAS,
                    "minReadySeconds": 0,
                    "template": {"spec": {"hostAliases": None}},
                }
            },
        )
        self._run(
            f"kubectl -n {self.namespace} set env deployment/frontend-proxy FRONTEND_HOST=frontend.{self.namespace}.svc.cluster.local"
        )
        self._rollout("frontend-proxy")
        # Restore the healthy public route even if a solver narrowed its selector.
        self._select_store(
            self._get("deployment", "frontend-proxy")["spec"]["selector"]["matchLabels"], "frontend-proxy"
        )
        self.wait_reads()
        # Start a new observable baseline. Repair requests from the preceding
        # episode are not mistaken for customer intent in the next episode.
        self.started_us = time.time_ns() // 1000
        self.expected_carts, self.events = {}, []
        self.unacknowledged = []
        actions, urls = [], self.cohort_urls()
        for user, cart in restored.items():
            actions.append({"op": "clear", "user": user, "url": urls[1]})
            actions.extend(
                {"op": "add", "user": user, "product": product, "quantity": quantity, "url": urls[1]}
                for product, quantity in cart.items()
            )
        self.client(actions, record=True)
        if not self.carts_match():
            raise RuntimeError("recovery did not restore consistent acknowledged carts")
        self.assert_native_history()
        self._patch(
            "configmap",
            self.OPERATIONS,
            {"data": {"acceptWrites": "true", "status": "running", "unacknowledgedTraceIds": "[]"}},
        )
        self.traffic_error = ""
        self.fault_injected = False

    def _run_product_probe(self):
        return self.hosts_mitigation_oracle._run_product_probe()

    def cleanup(self):
        self.stop_traffic()
        if self._original_cleanup is not None:
            self._original_cleanup()
