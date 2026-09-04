"""Mitigation oracle for the pod-local hosts-override problem on Astronomy Shop.

The generic :class:`~sregym.conductor.oracles.mitigation.MitigationOracle` only
looks at Deployment and pod health. That is not enough here: a pod-local
``/etc/hosts`` override keeps every control-plane signal green while the
frontend cannot reach its backend at all, so a health-only oracle reports
success on an unmitigated cluster.

This oracle therefore checks three independent things:

1. **Configuration** - the Deployment pod template declares no ``hostAliases``
   entry for the backend hostname.
2. **Runtime** - no live pod of that Deployment still carries such an entry.
   ``pod.spec.hostAliases`` is what the kubelet writes into ``/etc/hosts``, so
   this rejects a stale pod that survived a template-only edit, and it also
   rejects the "fix" of hand-editing ``/etc/hosts`` inside a running container
   (which leaves the template, and therefore the next pod, still poisoned).
3. **Function** - a fresh request through the edge proxy actually returns
   catalog data, i.e. the frontend really can reach the backend again.
"""

import contextlib
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class StaleHostAliasesMitigationOracle(Oracle):
    """Verify the hosts override is gone from config *and* runtime, and traffic works."""

    importance = 1.0
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
            if self._rollout_complete(deployment):
                return deployment
            if time.monotonic() >= deadline:
                return None

            time.sleep(self.poll_interval_seconds)
            deployment = self.problem.kubectl.get_deployment(
                deployment.metadata.name,
                self.problem.namespace,
            )

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

    def _has_ready_endpoint(self) -> bool:
        endpoints = self.problem.kubectl.core_v1_api.read_namespaced_endpoints(
            name=self.deployment_name,
            namespace=self.problem.namespace,
        )
        return any(subset.addresses for subset in endpoints.subsets or [])

    # ── functional probe ───────────────────────────────────────────────
    def _run_product_probe(self) -> bool:
        """Fetch the catalog through the edge proxy, which routes via the frontend."""
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
            "set -eu; "
            f"wget -q -T {self.request_timeout_seconds} -t 1 -O /tmp/products '{url}'; "
            f"grep -q '{self.expected_product_id}' /tmp/products; "
            "echo PRODUCTS_OK"
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
            return phase == "Succeeded" and "PRODUCTS_OK" in logs
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
        print("== Hosts Override Mitigation Evaluation ==")
        results = {
            "success": False,
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
            results["reason"] = f"deployment '{self.deployment_name}' is scaled to zero"
            print(f"[FAIL] {results['reason']}")
            return results

        settled = self._wait_for_current_rollout(deployment)
        if settled is None:
            results["reason"] = f"deployment '{self.deployment_name}' did not complete its current rollout"
            print(f"[FAIL] {results['reason']}")
            return results
        deployment = settled
        results["rollout_complete"] = True

        if self._template_override(deployment):
            results["reason"] = (
                f"the pod template of '{self.deployment_name}' still overrides "
                f"'{self.backend_hostname}' with a static hosts entry"
            )
            print(f"[FAIL] {results['reason']}")
            return results
        results["template_override_removed"] = True

        offending_pods = self._pods_with_override(deployment)
        if offending_pods:
            results["reason"] = (
                f"pod(s) {', '.join(sorted(offending_pods))} still carry a static hosts entry "
                f"for '{self.backend_hostname}'"
            )
            print(f"[FAIL] {results['reason']}")
            return results
        results["no_pod_carries_override"] = True

        try:
            if not self._has_ready_endpoint():
                results["reason"] = f"service '{self.deployment_name}' has no ready endpoint"
                print(f"[FAIL] {results['reason']}")
                return results
        except ApiException as exc:
            results["reason"] = f"could not read endpoints for '{self.deployment_name}': {exc}"
            print(f"[FAIL] {results['reason']}")
            return results
        results["ready_endpoint"] = True

        results["product_probe_succeeded"] = self._run_product_probe()
        if not results["product_probe_succeeded"]:
            results["reason"] = f"a fresh {self.product_path} request did not return catalog data"
            print(f"[FAIL] {results['reason']}")
            return results

        results["success"] = True
        results["reason"] = "the hosts override is gone and catalog requests succeed"
        print("[PASS] Mitigation Result: Pass")
        return results
