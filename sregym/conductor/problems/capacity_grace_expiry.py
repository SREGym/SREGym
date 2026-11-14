"""Usage-based capacity enforcement removes a shared login dependency once its grace period ends.

Modelled on the Google authentication outage of 14 December 2020
(https://status.cloud.google.com/incident/zall/20013): a quota migration left
the User ID Service's usage reported as zero, a grace period on enforcement hid
the error, and when it expired the quota automation acted on the wrong number.
"""

import time
from contextlib import contextmanager

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.capacity_grace_expiry_mitigation import CapacityGraceExpiryMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.base import Application
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

# Everything the agent can read (names, labels, the script, its output) stays
# neutral: nothing below may describe what goes wrong.
_CONTROLLER_SCRIPT = r"""
set -u
while true; do
  policy=$(kubectl get configmap "$POLICY_CONFIGMAP" -o go-template='{{index .data "enforcement"}} {{index .data "effective_from"}} {{index .data "sync_interval_seconds"}}' 2>/dev/null) || policy=""
  set -- $policy
  mode="${1:-}"
  start="${2:-}"
  interval="${3:-}"
  case "$interval" in ''|*[!0-9]*) interval=10 ;; esac
  [ "$interval" -lt 1 ] && interval=1
  case "$start" in ''|*[!0-9]*) mode="" ;; esac
  if [ "$mode" = "enabled" ] && [ "$(date +%s)" -ge "$start" ]; then
    kubectl get configmap "$USAGE_CONFIGMAP" -o go-template='{{range $k, $v := .data}}{{$k}} {{$v}}{{"\n"}}{{end}}' 2>/dev/null |
      while read -r name cap; do
        case "$cap" in ''|*[!0-9]*) continue ;; esac
        cur=$(kubectl -n "$TARGET_NAMESPACE" get deployment "$name" -o jsonpath='{.spec.replicas}' 2>/dev/null) || continue
        case "$cur" in ''|*[!0-9]*) continue ;; esac
        if [ "$cur" -gt "$cap" ]; then
          kubectl -n "$TARGET_NAMESPACE" scale deployment "$name" --replicas="$cap" >/dev/null 2>&1
        fi
      done
  fi
  sleep "$interval"
done
"""


class CapacityGraceExpiry(Problem):
    controller_name = "capacity-controller"
    controller_namespace = "capacity-system"
    app_namespace = "hotel-reservation"
    usage_configmap = "capacity-usage"
    policy_configmap = "capacity-policy"
    # bitnami/kubectl is no longer pulled freely from Docker Hub.
    controller_image = "alpine/k8s:1.28.3"
    grace_seconds = 60
    sync_interval_seconds = 10
    inject_timeout_seconds = 300
    poll_interval_seconds = 3

    def __init__(
        self,
        app_name: str = "hotel_reservation",
        faulty_service: str = "user",
        *,
        app: Application | None = None,
    ):
        if app_name != "hotel_reservation":
            raise ValueError(f"Unsupported app name: {app_name}")
        self.app = app if app is not None else HotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = faulty_service
        self._original_replicas = None
        self._effective_from = None

        self.root_cause = self.build_structured_root_cause(
            component=f"{self.controller_name} Deployment ({self.controller_namespace})",
            namespace=self.controller_namespace,
            description=(
                f"The `{self.controller_name}` Deployment in `{self.controller_namespace}` caps each deployment in "
                f"`{self.namespace}` at the replica count recorded for it in ConfigMap `{self.usage_configmap}`. "
                f"That usage record is wrong for `{self.faulty_service}`: it reports 0. Enforcement was held back by "
                f"a grace period (`effective_from` in ConfigMap `{self.policy_configmap}`); once it ended the "
                f"controller scaled deployment `{self.faulty_service}` to 0 replicas and keeps re-applying that on "
                f"every sync. `{self.faulty_service}` is the affected victim: with no pods behind it, every login "
                "through the frontend fails. A diagnosis that only reports the deployment at 0 replicas, or the "
                "failing logins, without identifying the capacity automation and the incorrect usage record it acts "
                "on is incomplete. A durable fix corrects the usage record, disables enforcement, or stops the "
                f"controller, and restores `{self.faulty_service}` to at least one ready replica."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = CapacityGraceExpiryMitigationOracle(problem=self)
        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        dep = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        self._original_replicas = dep.spec.replicas or 1

        self._teardown_controller()
        created_at = self._create_namespace()
        self._effective_from = created_at + self.grace_seconds
        self._create_rbac()
        self._create_configmaps()
        self._create_controller()
        self._wait_for_target_down(timeout=self.grace_seconds + self.inject_timeout_seconds)
        print(f"Service: {self.faulty_service} | controller={self.controller_namespace}/{self.controller_name}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self._teardown_controller()
        self._restore_replicas()
        print(f"Recovered: removed {self.controller_name} and restored deployment/{self.faulty_service}\n")

    # -- injection -----------------------------------------------------------------

    def _create_namespace(self) -> int:
        """Create the controller namespace; return its creation time (cluster clock, epoch seconds)."""
        body = {"metadata": {"name": self.controller_namespace}}
        created = self.kubectl.core_v1_api.create_namespace(body=body)
        return int(created.metadata.creation_timestamp.timestamp())

    def _usage_data(self) -> dict[str, str]:
        deployments = self.kubectl.list_deployments(self.namespace).items
        usage = {d.metadata.name: str(d.spec.replicas if d.spec.replicas is not None else 1) for d in deployments}
        usage[self.faulty_service] = "0"
        return usage

    def _create_configmaps(self):
        core = self.kubectl.core_v1_api
        labels = {"app": self.controller_name}
        core.create_namespaced_config_map(
            self.controller_namespace,
            {"metadata": {"name": self.usage_configmap, "labels": labels}, "data": self._usage_data()},
        )
        core.create_namespaced_config_map(
            self.controller_namespace,
            {
                "metadata": {"name": self.policy_configmap, "labels": labels},
                "data": {
                    "enforcement": "enabled",
                    "effective_from": str(self._effective_from),
                    "sync_interval_seconds": str(self.sync_interval_seconds),
                },
            },
        )

    def _create_rbac(self):
        core, rbac = self.kubectl.core_v1_api, client.RbacAuthorizationV1Api()
        name = self.controller_name
        subject = {"kind": "ServiceAccount", "name": name, "namespace": self.controller_namespace}
        core.create_namespaced_service_account(self.controller_namespace, {"metadata": {"name": name}})
        rbac.create_namespaced_role(
            self.controller_namespace,
            {
                "metadata": {"name": name},
                "rules": [{"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get", "list", "watch"]}],
            },
        )
        rbac.create_namespaced_role_binding(
            self.controller_namespace,
            {
                "metadata": {"name": name},
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
                "subjects": [subject],
            },
        )
        with self._ignore_conflict():
            rbac.create_namespaced_role(
                self.namespace,
                {
                    "metadata": {"name": name},
                    "rules": [
                        {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get", "list"]},
                        {
                            "apiGroups": ["apps"],
                            "resources": ["deployments/scale"],
                            "verbs": ["get", "patch", "update"],
                        },
                    ],
                },
            )
        with self._ignore_conflict():
            rbac.create_namespaced_role_binding(
                self.namespace,
                {
                    "metadata": {"name": name},
                    "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
                    "subjects": [subject],
                },
            )

    def _create_controller(self):
        labels = {"app": self.controller_name}
        container = {
            "name": "controller",
            "image": self.controller_image,
            "command": ["sh", "-c", _CONTROLLER_SCRIPT],
            "env": [
                {"name": "TARGET_NAMESPACE", "value": self.namespace},
                {"name": "USAGE_CONFIGMAP", "value": self.usage_configmap},
                {"name": "POLICY_CONFIGMAP", "value": self.policy_configmap},
            ],
            "resources": {"requests": {"cpu": "20m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}},
        }
        body = {
            "metadata": {"name": self.controller_name, "labels": labels},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "serviceAccountName": self.controller_name,
                        "terminationGracePeriodSeconds": 2,
                        "containers": [container],
                    },
                },
            },
        }
        self.kubectl.apps_v1_api.create_namespaced_deployment(self.controller_namespace, body)

    def _target_is_down(self) -> tuple[bool, str]:
        dep = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        pods = self.kubectl.list_pods(self.namespace).items
        live = [
            p
            for p in pods
            if (p.metadata.labels or {}).get("io.kompose.service") == self.faulty_service
            and p.metadata.deletion_timestamp is None
        ]
        state = f"spec.replicas={dep.spec.replicas} pods={len(live)}"
        return dep.spec.replicas == 0 and not live, state

    def _wait_for_target_down(self, timeout: float):
        deadline = time.monotonic() + timeout
        state = None
        while True:
            down, state = self._target_is_down()
            if down:
                print(f"Fault confirmed live: deployment/{self.faulty_service} {state}")
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(self.poll_interval_seconds)
        raise RuntimeError(f"deployment/{self.faulty_service} was not scaled down in time; last: {state}")

    # -- recovery ------------------------------------------------------------------

    def _restore_replicas(self):
        wanted = self._original_replicas or 1
        dep = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        if (dep.spec.replicas or 0) < wanted:
            self.kubectl.scale_deployment(self.faulty_service, self.namespace, wanted)

    def _teardown_controller(self):
        self._remove_controller(self.kubectl.core_v1_api, self.kubectl.apps_v1_api, self.namespace)

    @classmethod
    def cleanup_leftover_controller(cls):
        """Remove a controller left behind by a run that did not reach recover_fault()."""
        cls._remove_controller(client.CoreV1Api(), client.AppsV1Api(), cls.app_namespace)

    @classmethod
    def _remove_controller(cls, core, apps, app_namespace: str, timeout: float = 180, poll: float = 2):
        rbac = client.RbacAuthorizationV1Api(core.api_client)

        def ignore_404(fn, *args, **kwargs):
            try:
                fn(*args, **kwargs)
            except ApiException as e:
                if e.status != 404:
                    raise

        # Stop the controller before anything else so it cannot act mid-teardown.
        ignore_404(
            apps.delete_namespaced_deployment,
            cls.controller_name,
            cls.controller_namespace,
            grace_period_seconds=0,
            propagation_policy="Foreground",
        )
        ignore_404(rbac.delete_namespaced_role_binding, cls.controller_name, app_namespace)
        ignore_404(rbac.delete_namespaced_role, cls.controller_name, app_namespace)
        ignore_404(core.delete_namespace, cls.controller_namespace)

        deadline = time.monotonic() + timeout
        while True:
            try:
                core.read_namespace(cls.controller_namespace)
            except ApiException as e:
                if e.status == 404:
                    return
                raise
            if time.monotonic() >= deadline:
                raise RuntimeError(f"namespace {cls.controller_namespace} was not deleted within {timeout}s")
            time.sleep(poll)

    @contextmanager
    def _ignore_conflict(self):
        try:
            yield
        except ApiException as e:
            if e.status != 409:
                raise
