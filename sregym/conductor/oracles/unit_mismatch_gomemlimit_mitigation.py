import contextlib
import json
import re
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle

# Byte suffixes the Go runtime accepts for GOMEMLIMIT, per
# runtime/debug.SetMemoryLimit. Verified empirically against
# ghcr.io/open-telemetry/demo:2.2.0-checkout: SI suffixes ("16MB"),
# Kubernetes quantity suffixes ("16Mi"), lower-case suffixes ("16mib"),
# negatives, decimals, surrounding whitespace, and "EiB" are all rejected
# with "fatal error: malformed GOMEMLIMIT" before main() runs.
GO_MEMORY_SUFFIXES = {
    "B": 1,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
}

_GO_MEMORY_LIMIT_PATTERN = re.compile(r"^(\d+)(B|KiB|MiB|GiB|TiB)?$")

# "off" (lower case only) disables the limit and is accepted by the runtime,
# as is an unset variable. Both mean "no soft limit", which is the pre-fault
# default and therefore always has at least as much headroom as any explicit
# value.
GO_MEMORY_LIMIT_DISABLED = "off"

# Probe plumbing. checkout does not serve gRPC reflection, so grpcurl needs the
# descriptor supplied as a proto file; the placeholder lets each probe run use a
# fresh user id so repeated evaluations do not share cart state.
PROTO_FILE_NAME = "probe.proto"
PROBE_ID_PLACEHOLDER = "__PROBE_ID__"


def parse_go_memory_limit(value: str | None) -> int | None:
    """Return the byte value of a GOMEMLIMIT string, or None if Go would reject it.

    An unset variable and the literal "off" both parse to a disabled limit,
    reported as math.MaxInt64 the way the runtime documents its default.
    """
    if value is None or value == "":
        return 2**63 - 1
    if value == GO_MEMORY_LIMIT_DISABLED:
        return 2**63 - 1

    match = _GO_MEMORY_LIMIT_PATTERN.match(value)
    if match is None:
        return None

    amount, suffix = match.groups()
    return int(amount) * GO_MEMORY_SUFFIXES[suffix or "B"]


class UnitMismatchGomemlimitMitigation(Oracle):
    """Evaluate whether the Go memory limit is valid and the service really serves."""

    importance = 1.0
    rollout_timeout_seconds = 180
    probe_timeout_seconds = 120
    poll_interval_seconds = 2
    probe_image = "fullstorydev/grpcurl:v1.9.3"
    # A syntactically valid limit can still be absurd: the Go runtime accepts
    # "0" and "16B". Anything under this floor would leave the process GC
    # thrashing rather than serving, so it is not a repair.
    minimum_reasonable_limit_bytes = 1024**2

    def __init__(self, problem):
        """Capture problem constants needed to evaluate mitigation."""
        super().__init__(problem)
        self.baseline_replicas: int | None = None
        self.baseline_image: str | None = None
        self.baseline_resources: dict | None = None

    # ------------------------------------------------------------------
    # Baseline
    # ------------------------------------------------------------------

    def capture_baseline(self) -> None:
        """Record the healthy pre-fault deployment shape.

        Everything here is compared relatively, never against hard-coded chart
        values, so the oracle stays correct on a cluster whose chart differs
        from upstream.
        """
        deployment = self.problem.kubectl.get_deployment(self.problem.faulty_service, self.problem.namespace)
        container = self._target_container(deployment)
        self.baseline_replicas = self._desired_replicas(deployment)
        self.baseline_image = container.image
        self.baseline_resources = self._resources_as_dict(container)

    def _target_container(self, deployment):
        containers = deployment.spec.template.spec.containers
        return next(
            (item for item in containers if item.name == self.problem.faulty_service),
            containers[0],
        )

    @staticmethod
    def _resources_as_dict(container) -> dict:
        resources = container.resources
        if resources is None:
            return {"limits": {}, "requests": {}}
        return {
            "limits": dict(resources.limits or {}),
            "requests": dict(resources.requests or {}),
        }

    # ------------------------------------------------------------------
    # Deployment shape helpers
    # ------------------------------------------------------------------

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

    @staticmethod
    def _owned_by_active_replica_set(pod, active_replica_sets: set[str]) -> bool:
        return any(
            owner.kind == "ReplicaSet" and owner.name in active_replica_sets
            for owner in pod.metadata.owner_references or []
        )

    def _current_ready_pods(self) -> list:
        """Pods from the deployment's active ReplicaSet that are Running and Ready.

        Scoping to the active ReplicaSet is what stops a surviving pod from an
        older ReplicaSet, still carrying the pre-fault environment, from
        satisfying the oracle.
        """
        namespace = self.problem.namespace
        replica_sets = self.problem.kubectl.get_matching_replicasets(namespace, self.problem.faulty_service)
        active = {item.metadata.name for item in replica_sets if (item.spec.replicas or 0) > 0}
        if not active:
            return []

        ready = []
        for pod in self.problem.kubectl.list_pods(namespace).items:
            if pod.metadata.deletion_timestamp is not None:
                continue
            if not self._owned_by_active_replica_set(pod, active):
                continue
            statuses = pod.status.container_statuses or []
            if pod.status.phase == "Running" and statuses and all(status.ready for status in statuses):
                ready.append(pod)
        return ready

    def _configured_limit(self, deployment) -> tuple[str | None, bool]:
        """Return the container's configured GOMEMLIMIT and whether it is set at all."""
        container = self._target_container(deployment)
        for env in container.env or []:
            if env.name == self.problem.env_var:
                # A valueFrom reference cannot be resolved to a literal here;
                # treat it as present with an unknown value so it fails closed.
                if env.value is None:
                    return None, True
                return env.value, True
        return None, False

    # ------------------------------------------------------------------
    # Behavioral probe
    # ------------------------------------------------------------------

    def _grpcurl_args(self, probe_id: str, address: str, rpc: str, payload: dict, with_proto: bool) -> list[str]:
        args = ["-plaintext", "-max-time", "25"]
        if with_proto:
            args += ["-import-path", "/proto", "-proto", PROTO_FILE_NAME]
        args += ["-d", json.dumps(payload).replace(PROBE_ID_PLACEHOLDER, probe_id), address, rpc]
        return args

    def _probe_container(self, name: str, probe_id: str, spec: dict, with_proto: bool) -> client.V1Container:
        address = f"{spec['service']}.{self.problem.namespace}.svc.cluster.local:{self.problem.service_port}"
        return client.V1Container(
            name=name,
            image=self.probe_image,
            image_pull_policy="IfNotPresent",
            args=self._grpcurl_args(probe_id, address, spec["rpc"], spec["payload"], with_proto),
            volume_mounts=([client.V1VolumeMount(name="proto", mount_path="/proto")] if with_proto else None),
            resources=client.V1ResourceRequirements(
                requests={"cpu": "5m", "memory": "16Mi"},
                limits={"cpu": "200m", "memory": "64Mi"},
            ),
        )

    def _run_service_probe(self, deployment) -> bool:
        """Exercise the service's real API from a fresh pod.

        The target defines what a real request means: checkout places an actual
        order (an item is added to a cart first, in an init container), while a
        read-only service is exercised through its own read RPC. checkout does
        not serve gRPC reflection, so its descriptor is supplied as a proto file
        through a ConfigMap.
        """
        probe = self.problem.probe
        namespace = self.problem.namespace
        core_v1 = self.problem.kubectl.core_v1_api
        source_spec = deployment.spec.template.spec
        probe_id = f"probe-{time.time_ns()}"
        pod_name = f"gomemlimit-{probe_id}"[:63]
        config_map_name = f"gomemlimit-proto-{probe_id}"[:63]
        with_proto = probe.get("proto") is not None

        prerequisite = probe.get("prerequisite")
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name,
                namespace=namespace,
                labels={"app": "gomemlimit-probe"},
            ),
            spec=client.V1PodSpec(
                restart_policy="Never",
                active_deadline_seconds=self.probe_timeout_seconds,
                termination_grace_period_seconds=0,
                automount_service_account_token=False,
                dns_policy=source_spec.dns_policy,
                dns_config=source_spec.dns_config,
                image_pull_secrets=source_spec.image_pull_secrets,
                init_containers=(
                    [self._probe_container("prerequisite", probe_id, prerequisite, with_proto)]
                    if prerequisite
                    else None
                ),
                containers=[self._probe_container("probe", probe_id, probe, with_proto)],
                volumes=(
                    [
                        client.V1Volume(
                            name="proto",
                            config_map=client.V1ConfigMapVolumeSource(name=config_map_name),
                        )
                    ]
                    if with_proto
                    else None
                ),
            ),
        )

        try:
            if with_proto:
                core_v1.create_namespaced_config_map(
                    namespace=namespace,
                    body=client.V1ConfigMap(
                        metadata=client.V1ObjectMeta(name=config_map_name, namespace=namespace),
                        data={PROTO_FILE_NAME: probe["proto"]},
                    ),
                )
            core_v1.create_namespaced_pod(namespace=namespace, body=pod)

            deadline = time.monotonic() + self.probe_timeout_seconds
            phase = "Pending"
            while time.monotonic() < deadline:
                current = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
                phase = current.status.phase or "Pending"
                if phase in ("Succeeded", "Failed"):
                    break
                time.sleep(self.poll_interval_seconds)

            logs = core_v1.read_namespaced_pod_log(name=pod_name, namespace=namespace, container="probe")
            print(logs.strip()[:600])
            return phase == "Succeeded" and probe["success_token"] in logs
        except ApiException as exc:
            print(f"[FAIL] {probe['rpc']} probe failed: {exc}")
            return False
        finally:
            with contextlib.suppress(ApiException):
                core_v1.delete_namespaced_pod(
                    name=pod_name,
                    namespace=namespace,
                    grace_period_seconds=0,
                )
            if with_proto:
                with contextlib.suppress(ApiException):
                    core_v1.delete_namespaced_config_map(name=config_map_name, namespace=namespace)

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate(self, *args, **kwargs) -> dict:
        """Evaluate whether the Go memory limit is valid and the service serves."""
        print("== GOMEMLIMIT Unit Mismatch Mitigation Evaluation ==")
        results = {
            "success": False,
            "configured_limit": None,
            "limit_bytes": None,
            "replicas": None,
            "image_preserved": False,
            "resources_preserved": False,
            "current_ready_pods": 0,
            "probe_succeeded": False,
            "reason": "",
        }

        if self.baseline_replicas is None:
            results["reason"] = "no pre-fault baseline was captured"
            return results

        try:
            deployment = self.problem.kubectl.get_deployment(self.problem.faulty_service, self.problem.namespace)
        except Exception as exc:
            results["reason"] = f"deployment/{self.problem.faulty_service} does not exist: {exc}"
            return results

        # Structure. Catches deletion, scale-to-zero, and quietly shrinking the
        # deployment so fewer pods can fail.
        replicas = self._desired_replicas(deployment)
        results["replicas"] = replicas
        if replicas < 1:
            results["reason"] = f"deployment/{self.problem.faulty_service} is scaled to {replicas}"
            return results
        if replicas < self.baseline_replicas:
            results["reason"] = f"deployment/{self.problem.faulty_service} is scaled below its pre-fault replica count"
            return results

        # Unrelated configuration must survive the repair: swapping the image or
        # editing the memory limits is not a fix for a malformed env value.
        container = self._target_container(deployment)
        results["image_preserved"] = container.image == self.baseline_image
        results["resources_preserved"] = self._resources_as_dict(container) == self.baseline_resources
        if not results["image_preserved"]:
            results["reason"] = "the container image was changed from its pre-fault value"
            return results
        if not results["resources_preserved"]:
            results["reason"] = "the container resource requests or limits were changed from their pre-fault values"
            return results

        # The value itself: semantically valid rather than a match on the
        # original string, so any correct value the agent chooses is accepted.
        configured, is_set = self._configured_limit(deployment)
        results["configured_limit"] = configured if is_set else "<unset>"
        limit_bytes = parse_go_memory_limit(configured) if (configured is not None or not is_set) else None
        results["limit_bytes"] = limit_bytes
        if limit_bytes is None:
            results["reason"] = f"{self.problem.env_var} is still a value the Go runtime rejects"
            return results
        if limit_bytes < self.minimum_reasonable_limit_bytes:
            results["reason"] = (
                f"{self.problem.env_var} parses but is too small to run the service ({limit_bytes} bytes)"
            )
            return results

        # Runtime state, not just spec.
        deployment = self._wait_for_current_rollout(deployment)
        if deployment is None:
            results["reason"] = f"deployment/{self.problem.faulty_service} did not complete its current rollout"
            return results

        ready_pods = self._current_ready_pods()
        results["current_ready_pods"] = len(ready_pods)
        if not ready_pods:
            results["reason"] = (
                f"deployment/{self.problem.faulty_service} has no Running and Ready pod from its active ReplicaSet"
            )
            return results

        # Behaviour. The target defines no readiness probe, so a Running pod
        # proves nothing on its own; only a real request does.
        results["probe_succeeded"] = self._run_service_probe(deployment)
        if not results["probe_succeeded"]:
            results["reason"] = f"a fresh {self.problem.probe['rpc']} call did not succeed"
            return results

        results["success"] = True
        results["reason"] = "the Go memory limit is valid and the service completes a real request"
        print("Mitigation Result: Pass")
        return results
