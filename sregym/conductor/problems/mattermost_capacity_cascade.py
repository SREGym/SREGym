"""Slack-2021-inspired cascade: a misleading signal and automation that believes it.

The only thing injected is upstream latency. Everything after that is emergent:
the gateway's workers block instead of working, so its CPU *falls*, and the
pre-existing capacity automation reads that as spare capacity and removes
replicas from a service that is already shedding requests.

That is why this family is not another data-recovery task. There is nothing to
restore; the agent has to distrust a real measurement, stop automation that is
actively making things worse, and restore capacity that stays restored.
"""

import time

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mattermost_cascade_recovery import MattermostCascadeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.mattermost_cascade import MattermostCascade
from sregym.utils.decorators import mark_fault_injected

#: Added upstream latency. Far above the gateway's own per-request work, so its
#: workers spend their time blocked and aggregate CPU collapses.
UPSTREAM_DELAY_MS = 2500


class MattermostCapacityCascade(Problem):
    application_class = MattermostCascade
    #: Restoring capacity and waiting for the automation to settle takes longer
    #: than the shared cleanup drain allows.
    cleanup_timeout_seconds = 600

    def __init__(self, scale_tier="replicated"):
        super().__init__(self.application_class(scale_tier))
        self.kubectl = self.app.kubectl
        self.faulty_service, self.expected_service_port = "chat-gateway", 8080
        self.latency_injected = False
        self.observed = {}
        self.root_cause = self.build_structured_root_cause(
            component="chat-gateway",
            namespace=self.namespace,
            description="Added latency on the chat gateway's upstream call blocks its bounded worker pool, so the "
            "gateway sheds requests while its CPU utilization falls. The capacity automation is keyed on CPU and "
            "reads the idle-looking workers as spare capacity, so it removes gateway replicas and reverts manual "
            "scale-ups, deepening the outage. Recovery requires removing the latency and stopping the automation "
            "from holding capacity below the service's floor; CPU is the wrong capacity signal for a blocked pool.",
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(self, self.root_cause)
        self.mitigation_oracle = MattermostCascadeOracle(self)
        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        app = self.app
        self.observed["healthy"] = {
            "replicas": app.gateway_replicas(),
            "customer": app.probe_through_gateway(),
            "policy": app.read_control("scaler.json"),
        }
        app.write_control(
            "incident-notes.txt",
            # Symptoms and actions, not conclusions, and one wrong hypothesis --
            # which is what a real incident channel looks like. Naming the
            # misleading signal, or the fact that a manual scale-up gets
            # reverted, would hand over the task.
            "change window: upstream connection settings on the chat path were adjusted at 14:02\n"
            "on-call: customers report chat timing out; some requests come back 503\n"
            "on-call: mattermost itself answers fine when I curl it from inside the cluster\n"
            "on-call: feels like the database is struggling, asking the DB owner to look\n"
            "platform: parts of the capacity dashboard are not rendering\n",
        )

        self.latency_injected = True
        app.set_upstream_delay(UPSTREAM_DELAY_MS)

        # Wait for the cascade to become observable: the gateway saturates, CPU
        # falls, and the automation starts removing replicas on its own. Nothing
        # here scales anything; the emergent behaviour is the fault.
        deadline = time.monotonic() + 8 * app.scaler_interval
        while time.monotonic() < deadline:
            time.sleep(5)
            desired, _ = app.gateway_replicas()
            if desired < app.capacity_floor:
                self.observed["cascade"] = {
                    "replicas_desired": desired,
                    "decisions": app.scaler_decisions()[-5:],
                    "metrics": app.gateway_metrics(),
                }
                return
        raise RuntimeError(
            "The capacity automation did not reduce the gateway below its floor; "
            f"last decisions: {app.scaler_decisions()[-3:]}"
        )

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        if self.latency_injected and not self.mitigation_oracle.evaluate().get("success"):
            app = self.app
            # Reference recovery does both halves: remove the trigger, and stop
            # the automation holding capacity below the floor. Raising the policy
            # floor is one of several valid repairs.
            app.set_upstream_delay(0)
            app.scaler_policy(metric="saturation", min=app.capacity_floor)
            app.command("scale", "deployment/chat-gateway", f"--replicas={app.capacity_floor}")
            app.command("rollout", "status", "deployment/chat-gateway", "--timeout=300s", timeout=330)
            app.wait_for_gateway()
        self.latency_injected = False
