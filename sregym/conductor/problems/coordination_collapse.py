"""Roblox-2021-inspired coordination collapse: long horizon, broken tools, real cost.

Built because the three preceding families all scored 0% difficulty, solved in a
quarter of their budget. This one uses the levers none of them did:

- recovery has a measured floor and every phase is gated on the previous one
  settling, with premature action costing progress rather than merely failing;
- the convenient tools lie or hang, and the aggregated telemetry is circularly
  dependent on the thing that is broken;
- requests lost are lost permanently, and forcing progress by destroying members
  leaves damage no later action repairs.

Nothing here is injected as a flag. The fault is the coordination service's own
state -- watch subscriptions far over budget and a large compaction debt -- and
the degradation is computed from it.
"""

import time

from sregym.conductor.oracles.coordination_recovery import CoordinationRecoveryOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.coordination_cluster import RECOMMENDED_AGENT_TIMEOUT, CoordinationCluster
from sregym.service.apps.incident_runtime import coordination_store
from sregym.utils.decorators import mark_fault_injected


class CoordinationCollapse(Problem):
    application_class = CoordinationCluster
    #: The reference recovery waits out every gate, so cleanup needs more than
    #: the shared drain allows.
    cleanup_timeout_seconds = 1800
    #: Surfaced for the campaign runner and documented in the family's page: a
    #: 900-second budget is below this incident's own recovery floor plus any
    #: time to diagnose it, so running it at 900s would measure the budget.
    recommended_agent_timeout_seconds = RECOMMENDED_AGENT_TIMEOUT

    def __init__(self, scale_tier="single"):
        super().__init__(self.application_class(scale_tier))
        self.kubectl = self.app.kubectl
        self.faulty_service, self.expected_service_port = "coordinator", 8080
        self.collapsed = False
        self.observed = {}
        self.root_cause = self.build_structured_root_cause(
            component="coordinator",
            namespace=self.namespace,
            description="Watch subscriptions far above budget amplify every coordination write, holding leader "
            "write latency over its health threshold, so the leader cannot hold a term and every election "
            "re-reads the store. Dependent services resolve backends through it and fail; the aggregated metrics "
            "collector discovers its targets through it and reports nothing. Recovery requires shedding streaming "
            "load first, then compacting, rebuilding stale scheduler state, letting caches warm, and admitting "
            "traffic in held steps. Restarting the service cannot help: the degradation is its persisted data.",
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(self, self.root_cause)
        self.mitigation_oracle = CoordinationRecoveryOracle(self)
        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        """Confirm the collapse the deployed state already represents.

        The application deploys already degraded, because the incident is its
        persisted data rather than an event. Injection's job is to prove the
        collapse is real and observable before an agent is charged for it.
        """
        app = self.app
        truth = app.wait_for_coordinator()
        if truth["leader_healthy"]:
            raise RuntimeError("Coordination cluster is healthy; the incident state did not deploy")
        if truth["write_latency_ms"] <= truth["latency_budget_ms"]:
            raise RuntimeError("Write latency is within budget; the amplification did not take effect")
        if truth["watch_subscriptions"] <= coordination_store.WATCH_BUDGET:
            raise RuntimeError("Watch subscriptions are within budget; there is no load to shed")

        # The customer-visible symptom, and the stale tool that hides it.
        customer = app.customer_probe()
        reported = app.coordinator_request("/status")
        if not reported.get("stale") or not reported.get("healthy"):
            raise RuntimeError("The status endpoint is not serving its pre-incident snapshot")

        # The circular observability failure: the aggregated collector resolves
        # its targets through the service it is meant to be reporting on.
        metrics_blind = True
        try:
            app.service_metrics("discovery-metrics")
            metrics_blind = False
        except Exception:
            pass

        self.collapsed = True
        self.observed["collapse"] = {
            "write_latency_ms": truth["write_latency_ms"],
            "latency_budget_ms": truth["latency_budget_ms"],
            "watch_subscriptions": truth["watch_subscriptions"],
            "serve_capacity_fraction": truth["serve_capacity_fraction"],
            "customer": customer,
            "status_endpoint_stale": True,
            "aggregated_metrics_blind": metrics_blind,
            "recovery_floor_seconds": app.recovery_floor_seconds,
        }

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        if self.collapsed and not self.mitigation_oracle.evaluate().get("success"):
            app = self.app
            # If an agent destroyed a majority there is nothing to recover, and
            # saying so plainly is better than pretending cleanup succeeded.
            truth = app.truth()
            if truth["quorum_lost"]:
                self.collapsed = False
                raise RuntimeError(
                    "Quorum was permanently destroyed during this attempt; "
                    f"members {truth['destroyed_members']} cannot be restored"
                )
            app.reference_recovery()
            deadline = time.monotonic() + 120
            while app.truth()["serve_capacity_fraction"] < 1.0:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Reference recovery did not restore full serve capacity")
                time.sleep(5)
        self.collapsed = False
