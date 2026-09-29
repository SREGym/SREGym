"""Check persistent routing and acknowledged cart state, not just pod health.

An unused host alias is acceptable: a persistent FQDN route can bypass it.
For the cart cutover, all serving edges must agree on recovered customer state
and new mutations, both before and after replacement of the callers.
"""

import contextlib
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.mitigation import MitigationOracle


class ServingCapacityError(RuntimeError):
    """A required healthy edge is absent from the public serving route."""


class CartRequestError(RuntimeError):
    """A real application request failed, rather than the Kubernetes command."""


def cart_operations(traces, customers, unacknowledged=()):
    """Reconstruct successful native cart RPCs without counting their child spans."""
    events, seen = [], set()
    for trace in traces:
        if trace["traceID"] in unacknowledged:
            continue
        for span in trace["spans"]:
            identity = (trace["traceID"], span["spanID"])
            tags = {tag["key"]: tag["value"] for tag in span.get("tags", [])}
            method = span["operationName"].rsplit("/", 1)[-1]
            user = tags.get("app.user.id")
            if user not in customers or method not in ("AddItem", "EmptyCart") or identity in seen:
                continue
            if tags.get("span.kind") != "server":
                continue
            if (
                tags.get("error")
                or tags.get("otel.status_code") == "ERROR"
                or str(tags.get("rpc.grpc.status_code", 0)) != "0"
            ):
                continue
            seen.add(identity)
            operation = (user, "clear", None, None)
            if method == "AddItem":
                operation = (user, "add", tags["app.product.id"], int(tags["app.product.quantity"]))
            events.append((span["startTime"], identity, operation))
    return [operation for _, _, operation in sorted(events)]


class StaleHostAliasesMitigationOracle(MitigationOracle):
    """Verify functional recovery while accepting equivalent persistent routes."""

    importance = 1.0
    FAILURE_CLASSES = {
        "serving_capacity_reduced": "agent_error",
        "cart_request_failed": "agent_error",
        "retired_route_dependency": "agent_error",
    }
    rollout_timeout_seconds = 180
    probe_timeout_seconds = 90
    poll_interval_seconds = 2
    request_timeout_seconds = 5
    probe_image = "busybox:1.36"
    edge_service = "frontend-proxy"
    product_path = "/api/products"
    expected_product_id = "OLJCESPC7Z"

    def __init__(self, problem):
        super().__init__(problem)
        self.deployment_name = problem.faulty_service
        self.backend_hostname = problem.target_backend

    def capture_baseline(self) -> None:
        super().capture_baseline()
        # These callers may be removed when their declared Service entrypoints
        # are retired. Public capacity and cart operations are checked below.
        for name in getattr(self.problem, "RETIRED_ROUTES", ()):
            self.replica_count.pop(name, None)

    # ── rollout helpers ────────────────────────────────────────────────
    @staticmethod
    def _desired_replicas(deployment) -> int:
        replicas = deployment.spec.replicas
        return 1 if replicas is None else replicas

    @classmethod
    def _rollout_complete(cls, deployment) -> bool:
        desired = cls._desired_replicas(deployment)
        if desired < 1:
            return False

        generation = deployment.metadata.generation or 0
        status = deployment.status
        return (
            (status.observed_generation or 0) >= generation
            and (status.replicas or 0) == desired
            and (status.updated_replicas or 0) == desired
            and (status.ready_replicas or 0) == desired
            and (status.available_replicas or 0) == desired
            and (status.unavailable_replicas or 0) == 0
        )

    def _wait_for_current_rollout(self, deployment):
        deadline = time.monotonic() + self.rollout_timeout_seconds
        while True:
            if self._ready_for_validation(deployment):
                return deployment
            if getattr(deployment.spec, "paused", False) and not getattr(self.problem, "_prepared", False):
                return None
            if time.monotonic() >= deadline:
                return None

            time.sleep(self.poll_interval_seconds)
            deployment = self.problem.kubectl.get_deployment(
                deployment.metadata.name,
                self.problem.namespace,
            )

    def _ready_for_validation(self, deployment):
        # A repaired legacy route can remain intentionally paused. Readiness
        # alone does not pass it: cart consistency and replacement Pods below
        # must prove the running route works without applying a pending template.
        if (
            getattr(self.problem, "_prepared", False)
            and deployment.metadata.name in self.problem.EDGES
            and getattr(deployment.spec, "paused", False)
        ):
            desired, status = self._desired_replicas(deployment), deployment.status
            return (
                desired >= 1
                and (status.observed_generation or 0) >= (deployment.metadata.generation or 0)
                and (status.replicas or 0) == desired
                and (status.ready_replicas or 0) == desired
                and (status.available_replicas or 0) == desired
                and (status.unavailable_replicas or 0) == 0
            )
        return self._rollout_complete(deployment)

    def _namespace_health(self):
        """Retain baseline and pod-health checks without rejecting a sound paused route."""
        namespace, kubectl = self.problem.namespace, self.problem.kubectl
        deployments = {dep.metadata.name: dep for dep in kubectl.list_deployments(namespace).items}
        for name in self.replica_count:
            if name not in deployments:
                return self.fail("required_deployment_missing", deployment=name, namespace=namespace)
        for name, deployment in deployments.items():
            desired = self._desired_replicas(deployment)
            if desired == 0:
                if name in self.replica_count:
                    return self.fail("required_deployment_scaled_to_zero", deployment=name, namespace=namespace)
                continue
            if self._wait_for_current_rollout(deployment) is None:
                return self.fail("deployment_replicas_unready", deployment=name, namespace=namespace)
        retired = {
            name
            for name in self.problem.RETIRED_ROUTES
            if name not in deployments or self._desired_replicas(deployments[name]) == 0
        }
        pods = [
            pod
            for pod in kubectl.list_pods(namespace).items
            if (pod.metadata.labels or {}).get("opentelemetry.io/name") not in retired
        ]
        if not pods:
            return self.fail("no_pods_found", namespace=namespace)
        return self.pods_unready(pods, namespace=namespace)

    # ── hosts-override checks ──────────────────────────────────────────
    def _aliases_for_backend(self, host_aliases) -> list:
        """Return the hostAliases entries that shadow the backend hostname.

        A bare Service name and its fully qualified forms all resolve to the
        same backend, so an alias for any of them is still an override.
        """
        shadowed = {
            self.backend_hostname,
            f"{self.backend_hostname}.{self.problem.namespace}",
            f"{self.backend_hostname}.{self.problem.namespace}.svc",
            f"{self.backend_hostname}.{self.problem.namespace}.svc.cluster.local",
        }
        matches = []
        for alias in host_aliases or []:
            hostnames = getattr(alias, "hostnames", None)
            if hostnames is None and isinstance(alias, dict):
                hostnames = alias.get("hostnames")
            if any(hostname in shadowed for hostname in hostnames or []):
                matches.append(alias)
        return matches

    def _template_override(self, deployment) -> list:
        return self._aliases_for_backend(deployment.spec.template.spec.host_aliases)

    @staticmethod
    def _pod_matches_selector(pod, selector: dict) -> bool:
        labels = pod.metadata.labels or {}
        return all(labels.get(key) == value for key, value in selector.items())

    def _pods_with_override(self, deployment) -> list[str]:
        """Return names of live Deployment pods whose /etc/hosts is still poisoned."""
        selector = deployment.spec.selector.match_labels or {}
        if not selector:
            return []

        offending = []
        for pod in self.problem.kubectl.list_pods(self.problem.namespace).items:
            if pod.metadata.deletion_timestamp is not None:
                continue
            if not self._pod_matches_selector(pod, selector):
                continue
            if self._aliases_for_backend(pod.spec.host_aliases):
                offending.append(pod.metadata.name)
        return offending

    def _has_ready_endpoint(self, service_name: str | None = None) -> bool:
        name = service_name or self.deployment_name
        endpoints = self.problem.kubectl.core_v1_api.read_namespaced_endpoints(
            name=name,
            namespace=self.problem.namespace,
        )
        return any(subset.addresses for subset in endpoints.subsets or [])

    # ── functional probe ───────────────────────────────────────────────
    def _run_product_probe(self) -> bool:
        """Fetch the catalog through the edge proxy, which routes via the frontend."""
        if getattr(self.problem, "_prepared", False):
            self.problem.cart_mismatches = []
        namespace = self.problem.namespace
        core_v1 = self.problem.kubectl.core_v1_api

        service = core_v1.read_namespaced_service(name=self.edge_service, namespace=namespace)
        service_ports = service.spec.ports or []
        if not service_ports:
            print(f"[FAIL] Service '{self.edge_service}' has no ports")
            return False

        port = service_ports[0].port
        url = f"http://{self.edge_service}.{namespace}.svc.cluster.local:{port}{self.product_path}"
        pod_name = f"catalog-availability-check-{time.time_ns()}"[:63]
        script = (
            f"for i in $(seq 1 10); do "
            f"if wget -q -T {self.request_timeout_seconds} -t 1 -O /tmp/products '{url}' 2>/dev/null && "
            f"grep -q '{self.expected_product_id}' /tmp/products; then "
            f"echo PRODUCTS_OK; exit 0; fi; "
            f"sleep 2; done; "
            f"wget -T {self.request_timeout_seconds} -t 1 -O /tmp/products '{url}' || true; "
            f"exit 1"
        )
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name,
                namespace=namespace,
                labels={"app": "catalog-availability-check"},
            ),
            spec=client.V1PodSpec(
                restart_policy="Never",
                automount_service_account_token=False,
                termination_grace_period_seconds=0,
                containers=[
                    client.V1Container(
                        name="probe",
                        image=self.probe_image,
                        image_pull_policy="IfNotPresent",
                        command=["sh", "-c", script],
                    )
                ],
            ),
        )

        try:
            core_v1.create_namespaced_pod(namespace=namespace, body=pod)
            deadline = time.monotonic() + self.probe_timeout_seconds
            phase = "Pending"
            while time.monotonic() < deadline:
                current = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
                phase = current.status.phase or "Pending"
                if phase in ("Succeeded", "Failed"):
                    break
                time.sleep(self.poll_interval_seconds)

            logs = core_v1.read_namespaced_pod_log(name=pod_name, namespace=namespace)
            print(logs.strip())
            healthy = phase == "Succeeded" and "PRODUCTS_OK" in logs
            if healthy and getattr(self.problem, "_prepared", False):
                healthy = self.problem.carts_match()
            return healthy
        except ApiException as exc:
            print(f"[FAIL] Catalog availability probe could not run: {exc}")
            return False
        finally:
            with contextlib.suppress(ApiException):
                core_v1.delete_namespaced_pod(
                    name=pod_name,
                    namespace=namespace,
                    grace_period_seconds=0,
                )

    # ── entry point ────────────────────────────────────────────────────
    def evaluate(self, *args, **kwargs) -> dict:
        try:
            results = self._evaluate()
            code = results.pop("reason_code", "connectivity_probe_failed")
            if results["success"]:
                results.pop("failure_class", None)
            else:
                # Keep the original human-readable reason for existing callers,
                # alongside a stable code and the shared failure classification.
                results["reason_code"] = code
                results["failure_class"] = self.fail(code)["failure_class"]
            return results
        except ServingCapacityError as exc:
            print(f"[FAIL] {exc}")
            return self.fail("serving_capacity_reduced", error=str(exc))
        except CartRequestError as exc:
            return self.fail("cart_request_failed", error=str(exc))
        except Exception as exc:
            return self.fail_from_exception(exc)

    def _evaluate(self) -> dict:
        print("== Hosts Override Mitigation Evaluation ==")
        migration = getattr(self.problem, "_prepared", False)
        if migration:
            self.problem.stop_traffic()
            health_failure = self._namespace_health()
            if health_failure is not None:
                health_failure["reason_code"] = health_failure["reason"]
                return health_failure
        results = {
            **self.fail("connectivity_probe_failed"),
            "deployment_exists": False,
            "rollout_complete": False,
            "template_override_removed": False,
            "no_pod_carries_override": False,
            "ready_endpoint": False,
            "product_probe_succeeded": False,
            "reason": "",
        }

        try:
            deployment = self.problem.kubectl.get_deployment(self.deployment_name, self.problem.namespace)
        except Exception as exc:
            results["reason"] = f"deployment '{self.deployment_name}' does not exist: {exc}"
            print(f"[FAIL] {results['reason']}")
            return results
        results["deployment_exists"] = True

        if self._desired_replicas(deployment) < 1:
            results["reason_code"] = "required_deployment_scaled_to_zero"
            results["reason"] = f"deployment '{self.deployment_name}' is scaled to zero"
            print(f"[FAIL] {results['reason']}")
            return results

        settled = self._wait_for_current_rollout(deployment)
        if settled is None:
            if getattr(deployment.spec, "paused", False):
                results["reason_code"] = "fault_still_present"
            results["reason"] = f"deployment '{self.deployment_name}' did not complete its current rollout"
            print(f"[FAIL] {results['reason']}")
            return results
        deployment = settled
        results["rollout_complete"] = self._rollout_complete(deployment)
        results["runtime_available"] = True

        try:
            if not self._has_ready_endpoint(self.deployment_name):
                results["reason"] = f"service '{self.deployment_name}' has no ready endpoint"
                print(f"[FAIL] {results['reason']}")
                return results
            if not self._has_ready_endpoint(self.backend_hostname):
                results["reason"] = f"service '{self.backend_hostname}' has no ready endpoint"
                print(f"[FAIL] {results['reason']}")
                return results
        except ApiException as exc:
            results["reason"] = f"could not read endpoints: {exc}"
            print(f"[FAIL] {results['reason']}")
            return results
        results["ready_endpoint"] = True

        # Populate alias checks as informational fields (not gate-keeping).
        has_template_override = bool(self._template_override(deployment))
        results["template_override_removed"] = not has_template_override

        offending_pods = self._pods_with_override(deployment)
        results["no_pod_carries_override"] = len(offending_pods) == 0

        # The functional probe is the authoritative pass/fail gate.
        # If the agent found an alternative fix (e.g. switching FRONTEND_HOST
        # to the FQDN so the short-name alias is bypassed), the probe will
        # succeed and the oracle should accept it.
        results["product_probe_succeeded"] = self._run_product_probe()

        if results["product_probe_succeeded"]:
            if migration:
                results["customer_state_restored"] = True
                results["cross_instance_operations"] = False
                results["persistent_after_replacement"] = False
                intake = self.problem._get("configmap", self.problem.OPERATIONS)["data"].get("acceptWrites")
                if intake != "true" or self.problem.traffic_error:
                    results["reason_code"] = (
                        "oracle_command_failed" if self.problem.traffic_error else "fault_still_present"
                    )
                    results["reason"] = "customer intake is paused or has an unresolved request failure"
                    return results
                for name in ("cart", "cart-retained", "frontend", "frontend-retained", *self.problem.EDGES):
                    try:
                        current = self.problem.kubectl.get_deployment(name, self.problem.namespace)
                    except ApiException as exc:
                        if exc.status == 404 and name in self.problem.RETIRED_ROUTES:
                            continue
                        raise
                    if name in self.problem.RETIRED_ROUTES and self._desired_replicas(current) == 0:
                        continue
                    if not self._ready_for_validation(current):
                        results["reason"] = f"deployment '{name}' has not converged before replacement validation"
                        return results
                for name in self.problem.EDGES:
                    if not self._has_ready_endpoint(name):
                        results["reason"] = f"edge '{name}' has no ready endpoint"
                        return results
                results["cross_instance_operations"] = self.problem.fresh_operations_work()
                if not results["cross_instance_operations"]:
                    results["reason_code"] = "fault_still_present"
                    results["reason"] = "new acknowledged cart changes differ between serving endpoints"
                    return results
                # This runs only after the live routes already agree; it cannot
                # complete a paused rollout or reconcile an unfinished repair.
                retirement_required = bool(getattr(self.problem, "fault_injected", False))
                if retirement_required:
                    results["retired_release_independent"] = False
                try:
                    retirement = (
                        self.problem.retired_routes_withdrawn() if retirement_required else contextlib.nullcontext()
                    )
                    with retirement:
                        self.problem.restart_callers()
                        results["persistent_after_replacement"] = (
                            self.problem.carts_match() and self.problem.fresh_operations_work()
                        )
                        if retirement_required:
                            results["retired_release_independent"] = results["persistent_after_replacement"]
                except CartRequestError as exc:
                    results["reason_code"] = (
                        "retired_route_dependency" if retirement_required else "cart_request_failed"
                    )
                    results["reason"] = f"post-replacement public cart request failed: {exc}"
                    return results
                if not results["persistent_after_replacement"]:
                    results["reason_code"] = "fault_still_present"
                    results["reason"] = "cart state or routing did not survive legacy withdrawal and caller replacement"
                    return results
            results["success"] = True
            results["reason"] = (
                "customer state and new cart operations agree across persistent routes"
                if migration
                else "catalog requests succeed through the edge proxy"
            )
            print("[PASS] Mitigation Result: Pass")
            return results

        # Probe failed — give a specific reason.
        if migration and getattr(self.problem, "cart_mismatches", []):
            results["reason_code"] = "fault_still_present"
            results["reason"] = "acknowledged customer carts are missing or inconsistent between serving routes"
        elif has_template_override:
            results["reason"] = (
                f"the pod template of '{self.deployment_name}' still overrides "
                f"'{self.backend_hostname}' with a static hosts entry"
            )
        elif offending_pods:
            results["reason"] = (
                f"pod(s) {', '.join(sorted(offending_pods))} still carry a static hosts entry "
                f"for '{self.backend_hostname}'"
            )
        else:
            results["reason"] = f"a fresh {self.product_path} request did not return catalog data"
        print(f"[FAIL] {results['reason']}")
        return results
