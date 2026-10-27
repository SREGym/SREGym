"""Coordination collapse caused by excess watches and uncompacted state.

The persisted load prevents a stable leader, interrupts dependent services, and
blocks service discovery for metrics. Recovery must shed load before restoring
coordination state and gradually admitting traffic.
"""

import time

from sregym.conductor.oracles.apps.coordination.recovery import CoordinationRecoveryOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.coordination.application import RECOMMENDED_AGENT_TIMEOUT, CoordinationApplication
from sregym.service.apps.coordination.runtime import coordination_store
from sregym.utils.decorators import mark_fault_injected


class CoordinationCollapseProblem(Problem):
    application_class = CoordinationApplication
    #: The reference recovery waits out every gate, so cleanup needs more than
    #: the shared drain allows.
    cleanup_timeout_seconds = 1800
    #: Includes time for diagnosis and each mandatory recovery stability window.
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
        """Verify healthy startup, then induce and check the service failures."""
        app = self.app
        before = app.wait_for_coordinator()
        if not before["leader_healthy"] or before["serve_capacity_fraction"] < 1.0:
            raise RuntimeError(f"Cluster did not deploy healthy and serving: {before}")

        app.induce_collapse()
        truth = app.truth()
        if truth["leader_healthy"]:
            raise RuntimeError("Coordination cluster is still healthy; the collapse did not take effect")
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
        self.observed["healthy_before"] = {
            "serve_capacity_fraction": before["serve_capacity_fraction"],
            "write_latency_ms": before["write_latency_ms"],
            "watch_subscriptions": before["watch_subscriptions"],
        }
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


# Existing class names remain available to callers.
CoordinationCollapse = CoordinationCollapseProblem
