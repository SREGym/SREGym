"""
Ungraceful Pod Termination — Discord March 2026 voice outage simulation.

Real-world story:
  On March 25, 2026, Discord suffered a 3-hour voice outage after a routine
  Kubernetes scale-down terminated 50% of session pods simultaneously.
  A safety check meant to wait for in-flight handoffs to complete caused the
  terminationGracePeriodSeconds to elapse before handoffs could begin.
  Kubernetes sent SIGKILL, dropping 17% of active sessions and cascading
  into downstream voice syncer failure.

  Reference: https://discord.com/blog/behind-the-scenes-of-the-3-25-26-voice-outage

Simulation:
  We reproduce the *initiating mechanism* of the incident — an in-process
  shutdown safety wait that outlives the grace period — with the same
  shutdown sequence Kubernetes actually performs (SIGTERM first, SIGKILL
  when the grace period expires). We do not claim to reproduce Discord's
  full downstream cascade.

  1. The `reservation` deployment's container command is wrapped in a
     shell supervisor with a SIGTERM trap. On SIGTERM, the wrapper logs
     that a session handoff has started, keeps the server running (still
     serving in-flight requests) for a handoff window, then stops the
     server cleanly. This mirrors the incident: the safety wait runs
     AFTER SIGTERM, inside the grace-period countdown — unlike a preStop
     hook, which runs before SIGTERM and models a different sequence.

  2. terminationGracePeriodSeconds is set far below the handoff window,
     so every pod termination SIGKILLs the server mid-handoff, resetting
     genuinely in-flight requests. Loss is measurable: the wrk2 workload
     and the frontend see connection resets / 5xx during every churn
     event, and pod logs show handoffs that start but never complete.

  3. Churn is RECURRING, not one-time: a `maintenance-controller`
     CronJob (its own ServiceAccount/Role) deletes 50% of the
     reservation pods every few minutes, simulating routine node
     maintenance / scale-down events. The fault is therefore continuously
     observable while the agent works, with a fresh error burst per event.
     An initial 50% scale-down is also fired at injection time so evidence
     exists immediately.

  The mitigation oracle is behavioral: it runs a test rollout under
  continuous live traffic sampling and passes iff terminations no longer
  drop in-flight requests. It does not require any particular fix, so
  raising the grace period, fixing the handoff coordination, or any other
  approach that makes terminations actually graceful all pass.
"""

import json
import os
import shlex
import tempfile

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.ungraceful_termination_mitigation import UngracefulTerminationMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

# Generic names — do not expose fault category to agent
_HANDOFF_SECONDS = 45        # in-process handoff wait after SIGTERM
_FAULT_GRACE_PERIOD = 1      # far below the handoff window -> SIGKILL mid-handoff
_FLEET_REPLICAS = 4
_SCALED_DOWN_REPLICAS = 2    # 50% simultaneous termination, as in the incident
_CHURN_CRONJOB = "maintenance-controller"
_CHURN_SCHEDULE = "*/3 * * * *"
_RESERVATION_LABEL = "io.kompose.service=reservation"
_KUBECTL_IMAGE = "bitnami/kubectl:1.32"


class UngracefulPodTermination(Problem):
    def __init__(self, app_name: str = "hotel_reservation"):
        self.faulty_service = "reservation"

        if app_name == "hotel_reservation":
            app = HotelReservation()
        else:
            raise ValueError(f"Unsupported app name: {app_name}")

        super().__init__(app=app)

        self.kubectl = KubeCtl()
        self._original_grace_period = 30
        self._original_replicas = 1
        self._original_command = None
        self._original_args = None
        self._container_name = None

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"The `{self.faulty_service}` service performs an in-process "
                f"session-handoff wait (~{_HANDOFF_SECONDS}s) after receiving "
                "SIGTERM, but the deployment's terminationGracePeriodSeconds is "
                f"set to {_FAULT_GRACE_PERIOD}s. Whenever a pod is terminated — "
                "which happens repeatedly, because routine maintenance events "
                "delete half of the fleet every few minutes — Kubernetes sends "
                "SIGKILL long before the handoff completes, abruptly resetting "
                "all in-flight requests. This surfaces as recurring bursts of "
                "connection-reset errors and elevated 5xx rates in the frontend "
                "and workload metrics during every pod churn event, and pod logs "
                "showing handoffs that start but never complete."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        # NOTE: the harness owns app deployment (it creates the problem,
        # performs cleanup, and then deploys). We only prepare the workload
        # manager here — deploying from the constructor causes a premature
        # double deployment.
        self.app.create_workload()
        self.mitigation_oracle = UngracefulTerminationMitigationOracle(
            problem=self,
            deployment_name=self.faulty_service,
            churn_cronjob=_CHURN_CRONJOB,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_deployment_json(self) -> dict:
        output = self.kubectl.exec_command(
            f"kubectl get deployment {self.faulty_service} -n {self.namespace} -o json"
        )
        return json.loads(output)

    def _patch_deployment(self, patch: dict) -> None:
        # Strategic merge patch (kubectl default): merges containers by
        # `name`. A JSON merge patch (--type=merge) would replace the
        # containers list wholesale and strip required fields like `image`.
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                json.dump(patch, f)
                tmp_path = f.name
            self.kubectl.exec_command(
                f"kubectl patch deployment {self.faulty_service} -n {self.namespace} "
                f"--patch-file {tmp_path}"
            )
        finally:
            if tmp_path is not None:
                os.unlink(tmp_path)

    def _apply_manifest(self, manifest: str) -> None:
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
                f.write(manifest)
                tmp_path = f.name
            self.kubectl.exec_command(f"kubectl apply -f {tmp_path}")
        finally:
            if tmp_path is not None:
                os.unlink(tmp_path)

    def _scale(self, replicas: int, wait: bool = True) -> None:
        self.kubectl.exec_command(
            f"kubectl scale deployment/{self.faulty_service} "
            f"-n {self.namespace} --replicas={replicas}"
        )
        if wait:
            self.kubectl.exec_command(
                f"kubectl rollout status deployment/{self.faulty_service} "
                f"-n {self.namespace} --timeout=180s"
            )

    def _live_pod_spec(self) -> dict:
        return self._get_deployment_json()["spec"]["template"]["spec"]

    # ------------------------------------------------------------------
    # SIGTERM-handoff wrapper (the fault mechanism)
    # ------------------------------------------------------------------

    def _handoff_wrapper_script(self, original_command: list, original_args: list) -> str:
        """Wrap the app under a shell supervisor whose SIGTERM handler
        performs the 'session handoff' wait BEFORE stopping the server.

        This reproduces the incident's shutdown sequence exactly:
        SIGTERM -> in-process safety wait (server still serving) ->
        grace period expires -> SIGKILL mid-handoff. A preStop hook cannot
        model this, because preStop runs before SIGTERM is delivered.
        """
        orig = " ".join(shlex.quote(part) for part in (original_command + original_args))
        return (
            f"{orig} &\n"
            "CHILD=$!\n"
            "handoff() {\n"
            '  echo "session handoff started: waiting for in-flight sessions to transfer"\n'
            f"  sleep {_HANDOFF_SECONDS}\n"
            '  echo "session handoff complete: stopping server"\n'
            '  kill -TERM "$CHILD" 2>/dev/null\n'
            "}\n"
            "trap handoff TERM\n"
            'wait "$CHILD"\n'
            'wait "$CHILD"\n'
        )

    def _verify_fault_applied(self) -> None:
        """KubeCtl.exec_command suppresses kubectl errors, so a rejected
        patch would otherwise go unnoticed. Verify against the live object
        and fail loudly if the fault is not actually present."""
        pod_spec = self._live_pod_spec()
        container = pod_spec["containers"][0]
        grace = pod_spec.get("terminationGracePeriodSeconds")
        command = container.get("command") or []

        wrapped = (
            len(command) == 3
            and command[0] == "/bin/sh"
            and "handoff" in command[2]
        )
        if grace != _FAULT_GRACE_PERIOD or not wrapped:
            raise RuntimeError(
                f"Fault injection patch was not applied to {self.faulty_service} "
                f"(grace={grace}, wrapper={'present' if wrapped else 'missing'}). "
                "Check kubectl patch output for API server rejections."
            )
        if not container.get("image"):
            raise RuntimeError(
                "Fault injection corrupted the container spec (image missing)"
            )

    # ------------------------------------------------------------------
    # Recurring churn (routine maintenance events)
    # ------------------------------------------------------------------

    def _churn_manifest(self) -> str:
        """A CronJob that deletes 50% of the reservation pods every few
        minutes, simulating recurring node-maintenance / scale-down
        events. This keeps the fault continuously observable instead of
        being a one-time event that finishes before the agent starts."""
        return f"""apiVersion: v1
kind: ServiceAccount
metadata:
  name: {_CHURN_CRONJOB}
  namespace: {self.namespace}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: {_CHURN_CRONJOB}
  namespace: {self.namespace}
rules:
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "list", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: {_CHURN_CRONJOB}
  namespace: {self.namespace}
subjects:
- kind: ServiceAccount
  name: {_CHURN_CRONJOB}
  namespace: {self.namespace}
roleRef:
  kind: Role
  name: {_CHURN_CRONJOB}
  apiGroup: rbac.authorization.k8s.io
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: {_CHURN_CRONJOB}
  namespace: {self.namespace}
spec:
  schedule: "{_CHURN_SCHEDULE}"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      activeDeadlineSeconds: 120
      template:
        spec:
          serviceAccountName: {_CHURN_CRONJOB}
          restartPolicy: Never
          containers:
          - name: maintenance
            image: {_KUBECTL_IMAGE}
            command: ["/bin/sh", "-c"]
            args:
            - |
              TOTAL=$(kubectl get pods -n {self.namespace} -l {_RESERVATION_LABEL} --field-selector=status.phase=Running -o name | wc -l)
              HALF=$(( (TOTAL + 1) / 2 ))
              kubectl get pods -n {self.namespace} -l {_RESERVATION_LABEL} --field-selector=status.phase=Running -o name \\
                | head -n "$HALF" \\
                | xargs -r kubectl delete -n {self.namespace} --wait=false
"""

    def _remove_churn(self) -> None:
        self.kubectl.exec_command(
            f"kubectl delete cronjob {_CHURN_CRONJOB} -n {self.namespace} --ignore-not-found"
        )
        self.kubectl.exec_command(
            f"kubectl delete rolebinding {_CHURN_CRONJOB} -n {self.namespace} --ignore-not-found"
        )
        self.kubectl.exec_command(
            f"kubectl delete role {_CHURN_CRONJOB} -n {self.namespace} --ignore-not-found"
        )
        self.kubectl.exec_command(
            f"kubectl delete serviceaccount {_CHURN_CRONJOB} -n {self.namespace} --ignore-not-found"
        )
        self.kubectl.exec_command(
            f"kubectl delete jobs -n {self.namespace} "
            f"-l batch.kubernetes.io/cronjob-name={_CHURN_CRONJOB} --ignore-not-found"
        )

    # ------------------------------------------------------------------
    # Fault lifecycle
    # ------------------------------------------------------------------

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")

        # Save original deployment state for recovery
        deployment = self._get_deployment_json()
        pod_spec = deployment["spec"]["template"]["spec"]
        container = pod_spec["containers"][0]
        self._original_grace_period = pod_spec.get("terminationGracePeriodSeconds", 30)
        self._original_replicas = deployment["spec"].get("replicas", 1)
        self._container_name = container["name"]
        self._original_command = container.get("command")
        self._original_args = container.get("args")

        original_command = self._original_command or [self.faulty_service]
        original_args = self._original_args or []

        # 1. Scale up to a fleet so a 50% event is meaningful
        print(f"⏫ Scaling {self.faulty_service} to {_FLEET_REPLICAS} replicas...")
        self._scale(_FLEET_REPLICAS)

        # 2. Inject: short grace period + in-process SIGTERM handoff wait
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "terminationGracePeriodSeconds": _FAULT_GRACE_PERIOD,
                        "containers": [
                            {
                                "name": self._container_name,
                                "command": [
                                    "/bin/sh",
                                    "-c",
                                    self._handoff_wrapper_script(
                                        original_command, original_args
                                    ),
                                ],
                                # command now embeds args; clear the field
                                "args": None,
                            }
                        ],
                    }
                }
            }
        }
        self._patch_deployment(patch)
        self._verify_fault_applied()

        # Wait for the faulted pods to roll out. (The pods being replaced
        # here still terminate under the ORIGINAL settings; the fault
        # becomes observable on the churn events below.)
        self.kubectl.exec_command(
            f"kubectl rollout status deployment/{self.faulty_service} "
            f"-n {self.namespace} --timeout=180s"
        )

        # 3. Install recurring churn so the fault stays observable
        print(f"🔁 Installing recurring maintenance churn ({_CHURN_SCHEDULE})...")
        self._apply_manifest(self._churn_manifest())

        # 4. Fire the initial incident event: terminate 50% of the fleet
        #    now, so evidence (5xx burst, incomplete-handoff logs) exists
        #    immediately.
        print(
            f"⏬ Scaling down {_FLEET_REPLICAS} → {_SCALED_DOWN_REPLICAS} "
            "(50% simultaneous termination)..."
        )
        self._scale(_SCALED_DOWN_REPLICAS, wait=False)

        print(
            f"Injected ungraceful termination fault on: {self.faulty_service} "
            f"(terminationGracePeriodSeconds={_FAULT_GRACE_PERIOD}, "
            f"SIGTERM handoff wait={_HANDOFF_SECONDS}s, recurring churn "
            f"every {_CHURN_SCHEDULE!r})"
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")

        # 1. Stop the recurring churn first so recovery isn't raced
        self._remove_churn()

        container_name = self._container_name
        if container_name is None:
            container_name = self._live_pod_spec()["containers"][0]["name"]

        # 2. Restore original grace period, command, and args. With a
        #    strategic merge patch, null DELETES a field, so this is
        #    correct whether or not the container originally set them.
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "terminationGracePeriodSeconds": self._original_grace_period,
                        "containers": [
                            {
                                "name": container_name,
                                "command": self._original_command,
                                "args": self._original_args,
                            }
                        ],
                    }
                }
            }
        }
        self._patch_deployment(patch)

        # 3. Restore the original replica count
        self._scale(self._original_replicas, wait=False)

        # 4. Wait for rollout to settle
        self.kubectl.exec_command(
            f"kubectl rollout status deployment/{self.faulty_service} "
            f"-n {self.namespace} --timeout=180s"
        )

        # 5. Verify recovery actually landed (exec_command may hide failures)
        pod_spec = self._live_pod_spec()
        container = pod_spec["containers"][0]
        grace = pod_spec.get("terminationGracePeriodSeconds", 30)
        if (
            grace != self._original_grace_period
            or container.get("command") != self._original_command
            or container.get("args") != self._original_args
        ):
            raise RuntimeError(
                "Fault recovery did not restore the original spec "
                f"(grace={grace}, command={container.get('command')})"
            )

        print(f"Recovered ungraceful termination fault on: {self.faulty_service}")
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}")