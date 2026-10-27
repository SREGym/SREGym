"""Slack Spine: images."""

from __future__ import annotations

import json
from pathlib import Path

from sregym.conductor.oracles.apps.slack_spine.images import (
    StoreStrictEventOracle,
    _CorrelatedImageOracle,
    _StatefulSetImageOracle,
)
from sregym.conductor.oracles.incorrect_image_mitigation import IncorrectImageMitigationOracle
from sregym.conductor.problems.apps.slack_spine.helpers import (
    fetch_parallel,
    role_pod_ips,
)
from sregym.conductor.problems.apps.slack_spine.manifests import APP, _rollout_status
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.environment import configure_port, make_app
from sregym.conductor.problems.incorrect_image import IncorrectImage, _OriginalImage
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.utils.decorators import mark_fault_injected


class IncorrectImageSlack(IncorrectImage):
    """svc-thread is rolled to a non-existent image tag (scale 0 -> patch -> 1, as the original)."""

    BAD_IMAGE = "app-image:latest"

    def __init__(self, faulty_service: str = "svc-thread"):
        super().__init__(app=make_app(APP))
        self.faulty_service = [faulty_service]
        self._original_images: dict[str, _OriginalImage] = {}
        configure_port(
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


class FaultyImageCorrelatedSlack(Problem):
    """A correlated bad rollout pins several TypeScript svc-* roles to the Go real-time image.

    As in the original (Hotel Reservation services pinned to the Social Network
    image), the image is real and pulls fine but is the wrong program: it has
    no Node.js service for the role, so the containers cannot serve.
    """

    def __init__(
        self, faulty_services: tuple[str, ...] = ("svc-message", "svc-channel", "svc-thread", "svc-workspace")
    ):
        super().__init__(app=make_app(APP))
        self.faulty_service = list(faulty_services)
        self._original_images: dict[str, _OriginalImage] = {}
        self.bad_image = "unresolved"  # the chart's Go-tier image, resolved at inject time
        configure_port(
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
        self.bad_image = (
            self.kubectl.get_deployment("dispatcher", self.namespace).spec.template.spec.containers[0].image
        )
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


class UpdateIncompatibleCorrelatedSlack(Problem):
    """Slack's Postgres StatefulSets (`db`, `db-replica`) are upgraded in place to postgres:17.

    Their PVCs hold PostgreSQL 16 data directories; a 17 server refuses to
    start on them, as mongo:8.0.14-rc0 did on the original's 4.4 data.
    """

    BAD_IMAGE = "postgres:17"

    def __init__(self, statefulsets: tuple[str, ...] = ("db", "db-replica")):
        super().__init__(app=make_app(APP))
        self.faulty_service = list(statefulsets)
        self.original_images: dict[str, tuple[str, str]] = {}
        configure_port(
            self,
            APP,
            component=f"statefulsets/{', '.join(self.faulty_service)}",
            description=(
                f"The PostgreSQL StatefulSets ({', '.join(self.faulty_service)}) were upgraded in place from "
                f"`postgres:16` to an incompatible major version (`{self.BAD_IMAGE}`). Their persistent volumes hold "
                'PostgreSQL 16 data directories, which a 17 server refuses to open ("database files are '
                'incompatible with server"), so the database pods crash-loop and every service that persists to '
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


class ImageSlowLoadSlack(Problem):
    """Analogue: a runtime flag injects a fixed 10s delay into one user-facing read path.

    The original flips flagd's ``imageSlowLoad`` to its ``10sec`` variant and
    Envoy delays every product image by 10s. Slack Spine's servicekit has the
    same kind of runtime toggle: ``store_consistency_strict`` (``PUT
    /admin/event``) makes each store operation of a role wait
    ``STORE_HOLD_MS``. Here the dose is 10s (a latent deploy-time env) and the
    toggle is pushed only onto svc-notification, so users' unread badges
    (``GET /unread``) take 10s and time out while everything else is fast.
    (Incident Arena tasks 009/010 push the same event onto auth, workspace and
    notification together with a 100-250 ms dose.)
    """

    HOLD_MS = "10000"

    def __init__(self, role: str = "notification"):
        super().__init__(app=make_app(APP))
        self.role = role
        self.faulty_service = f"svc-{role}"
        self.event = "store_consistency_strict"
        configure_port(
            self,
            APP,
            component=self.faulty_service,
            description=(
                f"The `{self.faulty_service}` deployment serves unread counts (`GET /unread`, the users' unread "
                "badges) with abnormally high latency: every request takes ~10 s, so clients time out while every "
                "other endpoint is fast and every pod is Running and Ready. Mechanism: the runtime consistency "
                f"toggle `{self.event}` was switched on for svc-{role} via `PUT /admin/event` "
                f'(`{{"name": "{self.event}", "active": true}}`; `GET /admin/event` lists it as active). While it '
                f"is active every Redis store operation of the role first waits `STORE_HOLD_MS`, which this "
                f"deployment sets to {self.HOLD_MS} ms. Mitigation: deactivate the event on the role (`PUT "
                f'/admin/event {{"name": "{self.event}", "active": false}}`), or otherwise stop the delayed path.'
            ),
            oracle_factory=StoreStrictEventOracle,
        )
        self.app.configure({"app": {"roles": {role: {"env": {"STORE_HOLD_MS": self.HOLD_MS}}}}})

    def _set(self, active: bool) -> None:
        pods = role_pod_ips(self, self.role)
        replies = fetch_parallel(
            self,
            [
                (n, f"http://{ip}:8000/admin/event", "PUT", {"name": self.event, "active": active})
                for n, ip in pods.items()
            ],
        )
        for pod, (status, body, _) in replies.items():
            if int(status) != 200:
                raise RuntimeError(f"PUT /admin/event on {pod} returned {status}: {body[:200]}")
            print(f"{pod}: active events {json.loads(body).get('active')}")

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self._set(True)

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self._set(False)
