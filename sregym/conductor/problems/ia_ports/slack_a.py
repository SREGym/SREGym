"""SREGym problems ported to Slack Spine (helper slack_a).

Each class reuses the original problem's mechanism (injector, oracle) and
replaces its constructor via :func:`ported`: the app, the target component, the
ground truth and the mitigation oracle (wrapped with app health). Targets are
chosen on the users' path: the load generator's session mix is dominated by
history reads (svc-message), then unread counts (svc-notification), sends
(svc-message -> svc-auth session check -> svc-channel authz -> svc-workspace
org-policy check) and thread views (svc-thread). svc-file and svc-search carry
~1% and ~0.4% of the traffic, too little for a fault there to reach users.
"""

from __future__ import annotations

import copy
import json
import re
import time
from pathlib import Path

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.compound import CompoundedOracle
from sregym.conductor.oracles.incorrect_image_mitigation import IncorrectImageMitigationOracle
from sregym.conductor.oracles.incorrect_port import IncorrectPortAssignmentMitigationOracle
from sregym.conductor.oracles.missing_env_variable_mitigation import MissingEnvVariableMitigationOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.oracles.scale_pod_zero_mitigation import ScalePodZeroMitigationOracle
from sregym.conductor.oracles.service_endpoint_mitigation import ServiceEndpointMitigationOracle
from sregym.conductor.oracles.sustained_readiness import SustainedReadinessOracle
from sregym.conductor.oracles.target_port_mitigation import TargetPortMisconfigMitigationOracle
from sregym.conductor.problems.faulty_image_correlated import FaultyImageCorrelated
from sregym.conductor.problems.incorrect_image import IncorrectImage, _OriginalImage
from sregym.conductor.problems.incorrect_port_assignment import IncorrectPortAssignment
from sregym.conductor.problems.init_container_dependency_hang import InitContainerDependencyHang
from sregym.conductor.problems.lite_ia.k8s import ported, reopen_connections
from sregym.conductor.problems.liveness_probe_misconfiguration import LivenessProbeMisconfiguration
from sregym.conductor.problems.missing_configmap import MissingConfigMap
from sregym.conductor.problems.missing_env_variable import MissingEnvVariable
from sregym.conductor.problems.missing_service import MissingService
from sregym.conductor.problems.scale_pod import ScalePodSocialNet
from sregym.conductor.problems.service_port_conflict import ServicePortConflict
from sregym.conductor.problems.target_port import K8STargetPortMisconfig
from sregym.conductor.problems.update_incompatible_correlated import UpdateIncompatibleCorrelated
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.utils.decorators import mark_fault_injected

APP = "slack_spine"

_NOISY_METADATA = ("creationTimestamp", "generation", "managedFields", "resourceVersion", "selfLink", "uid")


def _clean_manifest(obj: dict) -> dict:
    """A live object as a manifest that can be re-created after deletion."""
    manifest = copy.deepcopy(obj)
    manifest.pop("status", None)
    meta = manifest.setdefault("metadata", {})
    for field in _NOISY_METADATA:
        meta.pop(field, None)
    annotations = meta.get("annotations") or {}
    annotations.pop("kubectl.kubernetes.io/last-applied-configuration", None)
    annotations.pop("deployment.kubernetes.io/revision", None)
    if annotations:
        meta["annotations"] = annotations
    else:
        meta.pop("annotations", None)
    if manifest.get("kind") == "Service":
        spec = manifest.get("spec", {})
        for field in ("clusterIP", "clusterIPs"):
            spec.pop(field, None)
    return manifest


def _get(problem, kind: str, name: str) -> dict:
    return json.loads(problem.kubectl.exec_command_checked(f"kubectl get {kind} {name} -n {problem.namespace} -o json"))


def _apply(problem, manifest: dict) -> None:
    problem.kubectl.exec_command_checked(f"kubectl apply -n {problem.namespace} -f -", input_data=json.dumps(manifest))


def _rollout_status(problem, deployment: str, timeout_s: int = 300) -> None:
    problem.kubectl.exec_command(
        f"kubectl rollout status deployment/{deployment} -n {problem.namespace} --timeout={timeout_s}s"
    )


def _replace_pods(problem, deployment: str) -> None:
    """Scale 0 -> 1 so the only pod runs the current (faulty) template.

    A rolling update of a one-replica Deployment keeps the old pod serving
    until the new one is Ready; a fault whose pod never becomes Ready would
    otherwise stay invisible behind it.
    """
    selector = ",".join(
        f"{k}={v}"
        for k, v in problem.kubectl.get_deployment(deployment, problem.namespace).spec.selector.match_labels.items()
    )
    problem.kubectl.exec_command_checked(f"kubectl scale deployment {deployment} -n {problem.namespace} --replicas=0")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        pods = problem.kubectl.core_v1_api.list_namespaced_pod(problem.namespace, label_selector=selector).items
        if not pods:
            break
        time.sleep(3)
    problem.kubectl.exec_command_checked(f"kubectl scale deployment {deployment} -n {problem.namespace} --replicas=1")


# --------------------------------------------------------------------------- #
# incorrect_image
# --------------------------------------------------------------------------- #
class IncorrectImageSlack(IncorrectImage):
    """svc-thread is rolled to a non-existent image tag (scale 0 -> patch -> 1, as the original)."""

    BAD_IMAGE = "app-image:latest"

    def __init__(self, faulty_service: str = "svc-thread"):
        self.faulty_service = [faulty_service]
        self._original_images: dict[str, _OriginalImage] = {}
        ported(
            self,
            APP,
            component=f"deployment/{faulty_service}",
            description=(
                f"The `{faulty_service}` deployment (Slack Spine's thread role, serving thread views and replies) "
                f"was rolled to a non-existent image tag (`{self.BAD_IMAGE}`) instead of the `slack-app` image the "
                "other svc-* roles run. Its pod is stuck in ErrImagePull/ImagePullBackOff, the Service has no "
                "endpoints, and users' thread requests fail. The fix is to set the container image back to the "
                "app's real image."
            ),
            oracle_factory=lambda problem: IncorrectImageMitigationOracle(
                problem=problem, actual_images={faulty_service: self.BAD_IMAGE}
            ),
        )
        self.injector = ApplicationFaultInjector(namespace=self.namespace)


# --------------------------------------------------------------------------- #
# faulty_image_correlated
# --------------------------------------------------------------------------- #
class _CorrelatedImageOracle(IncorrectImageMitigationOracle):
    """IncorrectImageMitigationOracle whose bad image is resolved at injection time."""

    def __init__(self, problem):
        super().__init__(problem, actual_images={})

    def evaluate(self) -> dict:
        self.actual_images = {service: self.problem.bad_image for service in self.problem.faulty_service}
        return super().evaluate()


class FaultyImageCorrelatedSlack(FaultyImageCorrelated):
    """A correlated bad rollout pins several TypeScript svc-* roles to the Go real-time image.

    As in the original (Hotel Reservation services pinned to the Social Network
    image), the image is real and pulls fine but is the wrong program: it has
    no Node.js service for the role, so the containers cannot serve.
    """

    def __init__(
        self, faulty_services: tuple[str, ...] = ("svc-message", "svc-channel", "svc-thread", "svc-workspace")
    ):
        self.faulty_service = list(faulty_services)
        self._original_images: dict[str, _OriginalImage] = {}
        self.bad_image = "unresolved"  # the chart's Go-tier image, resolved at inject time
        ported(
            self,
            APP,
            component=f"deployments/{', '.join(self.faulty_service)}",
            description=(
                f"A correlated bad rollout pinned multiple core services ({', '.join(self.faulty_service)}) to the "
                "`slack-go` image (the Go real-time tier's image used by geodns/dispatcher/ws-gateway) instead of "
                "the `slack-app` Node.js image. That image does not contain the TypeScript service, so these "
                "containers cannot start the role and never become Ready, breaking message history, sends, "
                "channel authorization, workspace policy checks and threads. The fix is to restore the `slack-app` "
                "image on every affected deployment."
            ),
            oracle_factory=_CorrelatedImageOracle,
        )
        self.injector = ApplicationFaultInjector(namespace=self.namespace)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.bad_image = self.kubectl.get_deployment("dispatcher", self.namespace).spec.template.spec.containers[0].image
        for service in self.faulty_service:
            deployment = self.kubectl.get_deployment(service, self.namespace)
            container = deployment.spec.template.spec.containers[0]
            self._original_images.setdefault(
                service, _OriginalImage(deployment.metadata.uid, container.name, container.image)
            )
            self.injector.inject_incorrect_image(
                deployment_name=service, namespace=self.namespace, bad_image=self.bad_image
            )
            print(f"Service: {service} | image -> {self.bad_image}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        for service in self.faulty_service:
            self.injector.recover_incorrect_image(
                deployment_name=service, namespace=self.namespace, correct_image=self._original_images[service].image
            )
        for service in self.faulty_service:
            _rollout_status(self, service)


# --------------------------------------------------------------------------- #
# update_incompatible_correlated
# --------------------------------------------------------------------------- #
class _StatefulSetImageOracle(Oracle):
    """The StatefulSets no longer run the injected image, are fully rolled out, and every pod is Ready."""

    importance = 1.0

    def __init__(self, problem):
        super().__init__(problem)
        self.apps = MitigationOracle(problem)

    def capture_baseline(self) -> None:
        self.apps.capture_baseline()

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")
        api = self.problem.kubectl.apps_v1_api
        still_wrong, unready = {}, {}
        for name in self.problem.faulty_service:
            sts = api.read_namespaced_stateful_set(name, self.problem.namespace)
            image = sts.spec.template.spec.containers[0].image
            if image == self.problem.BAD_IMAGE:
                print(f"❌ StatefulSet {name} still uses {image}")
                still_wrong[name] = image
                continue
            status = sts.status
            desired = sts.spec.replicas if sts.spec.replicas is not None else 1
            if (
                (status.ready_replicas or 0) < desired
                or (status.updated_replicas or 0) < desired
                or (status.update_revision and status.current_revision != status.update_revision)
            ):
                print(f"❌ StatefulSet {name} rollout incomplete ({status.ready_replicas or 0}/{desired} ready)")
                unready[name] = status.ready_replicas or 0
            else:
                print(f"✅ StatefulSet {name} runs {image} and is Ready")
        if still_wrong:
            return self.fail("fault_still_present", statefulsets=still_wrong)
        if unready:
            return self.fail("statefulset_unready", statefulsets=unready)
        return self.apps.evaluate()


class UpdateIncompatibleCorrelatedSlack(UpdateIncompatibleCorrelated):
    """Slack's Postgres StatefulSets (`db`, `db-replica`) are upgraded in place to postgres:17.

    Their PVCs hold PostgreSQL 16 data directories; a 17 server refuses to
    start on them, as mongo:8.0.14-rc0 did on the original's 4.4 data.
    """

    BAD_IMAGE = "postgres:17"

    def __init__(self, statefulsets: tuple[str, ...] = ("db", "db-replica")):
        self.faulty_service = list(statefulsets)
        self.original_images: dict[str, tuple[str, str]] = {}
        ported(
            self,
            APP,
            component=f"statefulsets/{', '.join(self.faulty_service)}",
            description=(
                f"The PostgreSQL StatefulSets ({', '.join(self.faulty_service)}) were upgraded in place from "
                f"`postgres:16` to an incompatible major version (`{self.BAD_IMAGE}`). Their persistent volumes hold "
                "PostgreSQL 16 data directories, which a 17 server refuses to open (\"database files are "
                "incompatible with server\"), so the database pods crash-loop and every service that persists to "
                "Postgres (messages, channels, threads, files, workspace) fails. The fix is to roll the StatefulSets "
                "back to `postgres:16` and replace the stuck pods."
            ),
            oracle_factory=_StatefulSetImageOracle,
        )

    @property
    def _state_path(self) -> Path:
        return Path(f"/tmp/sregym-{self.namespace}-pg-images.json")

    def _set_image(self, name: str, container: str, image: str) -> None:
        patch = {"spec": {"template": {"spec": {"containers": [{"name": container, "image": image}]}}}}
        self.kubectl.apps_v1_api.patch_namespaced_stateful_set(name, self.namespace, patch)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        for name in self.faulty_service:
            sts = self.kubectl.apps_v1_api.read_namespaced_stateful_set(name, self.namespace)
            container = sts.spec.template.spec.containers[0]
            self.original_images.setdefault(name, (container.name, container.image))
        self._state_path.write_text(json.dumps(self.original_images))
        for name in self.faulty_service:
            self._set_image(name, self.original_images[name][0], self.BAD_IMAGE)
            print(f"StatefulSet: {name} | image -> {self.BAD_IMAGE}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        originals = self.original_images or {k: tuple(v) for k, v in json.loads(self._state_path.read_text()).items()}
        for name in self.faulty_service:
            container, image = originals[name]
            self._set_image(name, container, image)
            # A StatefulSet does not replace a pod that never became Ready
            # (forced rollback): delete it so the controller recreates it.
            self.kubectl.exec_command(f"kubectl delete pod {name}-0 -n {self.namespace} --wait=false")
        for name in self.faulty_service:
            self.kubectl.exec_command(f"kubectl rollout status statefulset/{name} -n {self.namespace} --timeout=300s")


# --------------------------------------------------------------------------- #
# incorrect_port_assignment
# --------------------------------------------------------------------------- #
_URL_PORT = re.compile(r"^(?P<scheme>[a-z][a-z0-9+.-]*://)?(?P<host>[^:/]+):(?P<port>\d+)(?P<rest>.*)$")


def _with_port(value: str, port: str) -> str:
    match = _URL_PORT.match(value)
    if not match:
        raise ValueError(f"Cannot find a host:port in {value!r}")
    return f"{match['scheme'] or ''}{match['host']}:{port}{match['rest']}"


class _UrlPortOracle(IncorrectPortAssignmentMitigationOracle):
    """The dependency address is a URL (``redis://redis:6379``): probe its host:port."""

    def _configured_address(self, deployment) -> str | None:
        value = super()._configured_address(deployment)
        match = _URL_PORT.match(value or "")
        return f"{match['host']}:{match['port']}" if match else value


class IncorrectPortAssignmentSlack(IncorrectPortAssignment):
    """svc-workspace's REDIS_URL points at the wrong Redis port.

    svc-workspace serves org settings from a Redis cache; svc-channel
    revalidates the org policy on svc-workspace for every message send, so
    sends fail with 503 ``authz_unavailable``. The role touches Redis only
    when serving (its startup and health endpoint do not), so the
    misconfigured pod is Ready and replaces the healthy one, as Astronomy
    Shop's checkout did. (svc-auth was not used: it loads its signing keys
    from Redis at startup, so its misconfigured pod never becomes Ready.)
    """

    def __init__(
        self,
        faulty_service: str = "svc-workspace",
        env_var: str = "REDIS_URL",
        correct_port: str = "6379",
        incorrect_port: str = "6380",
    ):
        self.faulty_service = faulty_service
        self.env_var = env_var
        self.correct_port = correct_port
        self.incorrect_port = incorrect_port
        self.tcp_dependency_probe = True
        ported(
            self,
            APP,
            component=f"deployment/{faulty_service}",
            description=(
                f"The `{env_var}` environment variable of deployment `{faulty_service}` points to the wrong Redis "
                f"port (`redis://redis:{incorrect_port}` instead of `redis://redis:{correct_port}`). Nothing listens "
                "there, so svc-workspace cannot reach its org-settings cache and `GET /orgs/:id/settings` fails. "
                "svc-channel revalidates the org policy on svc-workspace for every message send, so message posts "
                "fail with 503 `authz_unavailable` while the svc-workspace pod stays Running and Ready."
            ),
            oracle_factory=_UrlPortOracle,
        )

    def _set_port(self, port: str) -> None:
        deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        container = deployment.spec.template.spec.containers[0]
        env = [
            {"name": var.name, "value": _with_port(var.value, port)}
            for var in container.env or []
            if var.name == self.env_var
        ]
        if not env:
            raise ValueError(f"Environment variable '{self.env_var}' not found in '{self.faulty_service}'")
        patch = {"spec": {"template": {"spec": {"containers": [{"name": container.name, "env": env}]}}}}
        self.kubectl.patch_deployment(self.faulty_service, self.namespace, patch)
        _rollout_status(self, self.faulty_service)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self._set_port(self.incorrect_port)
        print(f"{self.faulty_service}: {self.env_var} -> port {self.incorrect_port}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self._set_port(self.correct_port)


# --------------------------------------------------------------------------- #
# missing_env_variable
# --------------------------------------------------------------------------- #
class MissingEnvVariableSlack(MissingEnvVariable):
    """REDIS_URL is removed from svc-notification, which refuses to start without it."""

    def __init__(self, faulty_service: str = "svc-notification", env_var: str = "REDIS_URL"):
        self.faulty_service = faulty_service
        self.app_name = APP
        self.env_var = env_var
        self.env_var_value = "redis://redis:6379"
        ported(
            self,
            APP,
            component=f"deployment/{faulty_service}",
            description=(
                f"The required environment variable `{env_var}` (`{self.env_var_value}`) was removed from deployment "
                f"`{faulty_service}`. The notification role keeps unread counters in Redis and refuses to start "
                "without it (`servicekit: role 'notification' requires REDIS_URL but it is not set`), so its pod "
                "crash-loops, the Service has no endpoints, and users' unread-count requests (and svc-message's "
                "notification fan-out) fail."
            ),
            oracle_factory=MissingEnvVariableMitigationOracle,
        )

    @mark_fault_injected
    def inject_fault(self):
        super().inject_fault()
        # The new pod never becomes Ready: replace the old one so it cannot mask the fault.
        _replace_pods(self, self.faulty_service)


# --------------------------------------------------------------------------- #
# scale_pod_zero
# --------------------------------------------------------------------------- #
class ScalePodZeroSlack(ScalePodSocialNet):
    def __init__(self, faulty_service: str = "svc-notification"):
        self.faulty_service = faulty_service
        ported(
            self,
            APP,
            component=f"deployment/{faulty_service}",
            description=(
                f"The deployment `{faulty_service}` is scaled to zero replicas, removing every pod of Slack Spine's "
                "notification role. Its Service has no endpoints, so users' unread-count requests (`GET /unread`) "
                "and svc-message's notification fan-out fail immediately."
            ),
            oracle_factory=ScalePodZeroMitigationOracle,
        )


# --------------------------------------------------------------------------- #
# liveness_probe_misconfiguration
# --------------------------------------------------------------------------- #
class LivenessProbeMisconfigurationSlack(LivenessProbeMisconfiguration):
    def __init__(self, faulty_service: str = "svc-workspace"):
        self.faulty_service = faulty_service
        self.app_name = APP
        ported(
            self,
            APP,
            component=faulty_service,
            description=(
                f"The deployment `{faulty_service}` has a misconfigured liveness probe that checks `/healthz` on port "
                "`8080` (the service listens on 8000) with failureThreshold 1, so Kubernetes kills and restarts its "
                "pod every few seconds and it ends in CrashLoopBackOff. svc-channel revalidates the org policy on "
                "svc-workspace for every message send, so sends fail intermittently or completely while the pod churns."
            ),
            # Between kills the pod is briefly Ready, so a single snapshot can
            # miss the restart loop: readiness must also hold for a minute.
            oracle_factory=lambda problem: CompoundedOracle(
                problem, MitigationOracle(problem), SustainedReadinessOracle(problem, buffer_period=30, sustained_period=60)
            ),
        )
        self.injector = VirtualizationFaultInjector(namespace=self.namespace)


# --------------------------------------------------------------------------- #
# init_container_dependency_hang
# --------------------------------------------------------------------------- #
class InitContainerDependencyHangSlack(InitContainerDependencyHang):
    """Faithful to the original: the new ReplicaSet hangs in Init while the old pod keeps serving."""

    def __init__(self, faulty_service: str = "svc-file"):
        self.faulty_service = faulty_service
        self.app_name = APP
        ported(
            self,
            APP,
            component=f"deployment/{faulty_service}",
            description=(
                f"Deployment `{faulty_service}` has an init container `wait-for-legacy-config` that runs `until "
                "nslookup legacy-config-service.<namespace>.svc.cluster.local; do sleep 5; done`. The Service "
                "`legacy-config-service` does not exist, so the loop never succeeds: every new pod is stuck in "
                "`Init:0/1` and the rollout stalls with zero updated/ready replicas in the new ReplicaSet while the "
                "old ReplicaSet's pod continues serving traffic. Mitigation: remove the broken init container or "
                "repoint it at a service that resolves."
            ),
            oracle_factory=MitigationOracle,
        )


# --------------------------------------------------------------------------- #
# k8s_target_port-misconfig
# --------------------------------------------------------------------------- #
class _TargetPortOracle(TargetPortMisconfigMitigationOracle):
    """The Service targets the container's port again, by name (``http``) or number (8000)."""

    def evaluate(self) -> dict:
        kubectl = self.problem.kubectl
        target_port = kubectl.get_service_json(self.problem.faulty_service, self.problem.namespace)["spec"]["ports"][
            0
        ]["targetPort"]
        if target_port not in self.problem.valid_target_ports:
            print("== Mitigation Evaluation ==")
            print(f"❌ Service {self.problem.faulty_service} targetPort is {target_port}")
            return self.fail("fault_still_present", target_port=target_port)
        # The original check compares against Social Network's 9090: hand it a
        # view where the (already validated) healthy port reads as 9090.
        original = kubectl.get_service_json

        def healthy_view(name, namespace, deserialize=True):
            data = original(name, namespace, deserialize)
            if name == self.problem.faulty_service:
                data["spec"]["ports"][0]["targetPort"] = 9090
            return data

        kubectl.get_service_json = healthy_view
        try:
            return super().evaluate()
        finally:
            kubectl.get_service_json = original


class K8STargetPortMisconfigSlack(K8STargetPortMisconfig):
    def __init__(self, faulty_service: str = "svc-workspace", client_deployment: str = "svc-channel"):
        self.faulty_service = faulty_service
        self.client_deployment = client_deployment
        self.from_port = "http"
        self.to_port = 9999
        self.valid_target_ports = ("http", 8000, "8000")
        ported(
            self,
            APP,
            component=f"service/{faulty_service}",
            description=(
                f"The Service `{faulty_service}` targetPort was changed from the container port `http` (8000) to "
                "`9999`, where no process listens. Requests reach the pods' IP on a closed port and fail with "
                "connection refused; svc-channel revalidates the org policy on svc-workspace for every message send, "
                "so sends fail with 503 `authz_unavailable` while every pod stays Running and Ready."
            ),
            oracle_factory=_TargetPortOracle,
        )

    def _set_target_port(self, value) -> None:
        ports = self.kubectl.get_service_json(self.faulty_service, self.namespace)["spec"]["ports"]
        ports[0]["targetPort"] = value
        patch = json.dumps([{"op": "replace", "path": "/spec/ports", "value": ports}])
        self.kubectl.exec_command_checked(
            f"kubectl patch service {self.faulty_service} -n {self.namespace} --type=json -p '{patch}'"
        )

    @mark_fault_injected
    def inject_fault(self):
        self._set_target_port(self.to_port)
        print(f"[FAULT INJECTED] {self.faulty_service} targetPort {self.from_port} -> {self.to_port}")
        # Kept-alive connections to the old target port would otherwise keep working.
        reopen_connections(self, self.client_deployment)

    @mark_fault_injected
    def recover_fault(self):
        self._set_target_port(self.from_port)
        print(f"[FAULT RECOVERED] {self.faulty_service}")


# --------------------------------------------------------------------------- #
# service_port_conflict
# --------------------------------------------------------------------------- #
class ServicePortConflictSlack(ServicePortConflict):
    def __init__(self, faulty_service: str = "svc-workspace"):
        self.faulty_service = faulty_service
        self.app_name = APP
        self.conflicting_port = 9100
        ported(
            self,
            APP,
            component=f"deployment/{faulty_service}",
            description=(
                f"The pod template of deployment `{faulty_service}` binds hostPort {self.conflicting_port}, which "
                "collides with the prometheus-node-exporter DaemonSet (observe namespace) that already occupies port "
                f"{self.conflicting_port} on every node. The new pod cannot be scheduled (FailedScheduling: didn't "
                "have free ports), the deployment has no available replica, and message sends fail because "
                "svc-channel revalidates the org policy on svc-workspace for every send."
            ),
            oracle_factory=MitigationOracle,
        )

    def _assert_port_taken(self) -> None:
        """The conflict needs a host-port holder (node-exporter) on every schedulable node."""
        holders = {
            pod.spec.node_name
            for pod in self.kubectl.core_v1_api.list_pod_for_all_namespaces().items
            if pod.spec.node_name
            and any(
                (port.host_port or (port.container_port if pod.spec.host_network else None)) == self.conflicting_port
                for container in pod.spec.containers
                for port in container.ports or []
            )
        }
        schedulable = {
            node.metadata.name
            for node in self.kubectl.core_v1_api.list_node().items
            if not any(taint.effect == "NoSchedule" for taint in node.spec.taints or [])
        }
        if not schedulable <= holders:
            raise RuntimeError(
                f"hostPort {self.conflicting_port} is free on {sorted(schedulable - holders)}: "
                "prometheus-node-exporter must run on every schedulable node for this fault"
            )

    @mark_fault_injected
    def inject_fault(self):
        self._assert_port_taken()
        super().inject_fault()


# --------------------------------------------------------------------------- #
# missing_service
# --------------------------------------------------------------------------- #
class MissingServiceSlack(MissingService):
    """The Service `svc-channel` is deleted; svc-message's per-send authz check can no longer resolve it."""

    def __init__(self, faulty_service: str = "svc-channel", client_deployment: str = "svc-message"):
        self.faulty_service = faulty_service
        self.client_deployment = client_deployment
        self.app_name = APP
        self.expected_service_port = 8000
        ported(
            self,
            APP,
            component=f"service/{faulty_service}",
            description=(
                f"The Kubernetes Service `{faulty_service}` has been deleted, so its name no longer resolves through "
                "cluster DNS although the svc-channel pods are still Running and Ready. svc-message resolves channel "
                "authorization on svc-channel for every message send, so sends fail with 503 `authz_unavailable`."
            ),
            # Every pod stays Ready (health endpoints do not call svc-channel),
            # so the generic check alone cannot see the fault: the Service must
            # exist again with reachable endpoints.
            oracle_factory=lambda problem: CompoundedOracle(
                problem, MitigationOracle(problem), ServiceEndpointMitigationOracle(problem)
            ),
        )

    @property
    def _backup_path(self) -> Path:
        return Path(f"/tmp/sregym-{self.namespace}-{self.faulty_service}-service.json")

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self._backup_path.write_text(json.dumps(_clean_manifest(_get(self, "service", self.faulty_service))))
        self.kubectl.exec_command_checked(f"kubectl delete service {self.faulty_service} -n {self.namespace}")
        print(f"Deleted service {self.faulty_service}")
        # Established keep-alive connections survive the deletion: make the caller re-resolve.
        reopen_connections(self, self.client_deployment)

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        _apply(self, json.loads(self._backup_path.read_text()))
        print(f"Recreated service {self.faulty_service}")


# --------------------------------------------------------------------------- #
# missing_configmap
# --------------------------------------------------------------------------- #
class MissingConfigMapSlack(MissingConfigMap):
    """The shared `app-config` ConfigMap is deleted and svc-message restarted (scale 0 -> 1, as the original)."""

    def __init__(self, faulty_service: str = "svc-message", configmap: str = "app-config"):
        self.faulty_service = faulty_service
        self.configmap = configmap
        self.app_name = APP
        ported(
            self,
            APP,
            component=faulty_service,
            description=(
                f"The ConfigMap `{configmap}` (the `app.yaml` role configuration that every svc-* deployment mounts "
                f"at /config) has been deleted. Pods still running keep their mounted copy, but deployment "
                f"`{faulty_service}` was restarted: its new pod cannot mount the volume (FailedMount: configmap "
                f'"{configmap}" not found) and stays in ContainerCreating, so the message role is down and users\' '
                "history reads and message sends fail."
            ),
            oracle_factory=MitigationOracle,
        )

    @property
    def _backup_path(self) -> Path:
        return Path(f"/tmp/sregym-{self.namespace}-{self.configmap}-configmap.json")

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self._backup_path.write_text(json.dumps(_clean_manifest(_get(self, "configmap", self.configmap))))
        self.kubectl.exec_command_checked(f"kubectl delete configmap {self.configmap} -n {self.namespace}")
        print(f"Deleted ConfigMap {self.configmap}")
        _replace_pods(self, self.faulty_service)

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        _apply(self, json.loads(self._backup_path.read_text()))
        self.kubectl.exec_command(f"kubectl rollout restart deployment {self.faulty_service} -n {self.namespace}")
        _rollout_status(self, self.faulty_service)


# Original registry id -> (ported problem id, problem class). Variants of one
# fault on different original apps map to the same port.
PORTS: dict[str, tuple[str, type]] = {
    "incorrect_image": ("incorrect_image_slack_spine", IncorrectImageSlack),
    "faulty_image_correlated": ("faulty_image_correlated_slack_spine", FaultyImageCorrelatedSlack),
    "update_incompatible_correlated": ("update_incompatible_correlated_slack_spine", UpdateIncompatibleCorrelatedSlack),
    "incorrect_port_assignment": ("incorrect_port_assignment_slack_spine", IncorrectPortAssignmentSlack),
    "missing_env_variable_astronomy_shop": ("missing_env_variable_slack_spine", MissingEnvVariableSlack),
    "k8s_target_port-misconfig": ("k8s_target_port-misconfig_slack_spine", K8STargetPortMisconfigSlack),
    **{
        f"liveness_probe_misconfiguration_{app}": (
            "liveness_probe_misconfiguration_slack_spine",
            LivenessProbeMisconfigurationSlack,
        )
        for app in ("astronomy_shop", "hotel_reservation", "social_network")
    },
    **{
        f"init_container_dependency_hang_{app}": (
            "init_container_dependency_hang_slack_spine",
            InitContainerDependencyHangSlack,
        )
        for app in ("hotel_reservation", "social_network", "astronomy_shop")
    },
    **{
        f"missing_configmap_{app}": ("missing_configmap_slack_spine", MissingConfigMapSlack)
        for app in ("hotel_reservation", "social_network")
    },
    **{
        f"missing_service_{app}": ("missing_service_slack_spine", MissingServiceSlack)
        for app in ("astronomy_shop", "hotel_reservation", "social_network")
    },
    "scale_pod_zero_social_net": ("scale_pod_zero_slack_spine", ScalePodZeroSlack),
    **{
        f"service_port_conflict_{app}": ("service_port_conflict_slack_spine", ServicePortConflictSlack)
        for app in ("astronomy_shop", "hotel_reservation", "social_network")
    },
}
