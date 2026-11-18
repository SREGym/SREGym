"""Shared edge gateway cascade, after Microsoft 365's outage of 12 November 2026.

A change to session admission, rolled out months earlier and clean ever since,
put every connection's admission behind one spinning lock with work that grows
with the number of live sessions. Two routine events then raise the load at
peak: about a tenth of the gateway units are out for maintenance, and chat
fails over from the alternate gateway onto the shared fleet. Admission tips
into contention on some units, the traffic manager ejects them and redirects
their traffic, the next units tip, and the loosened bulkheads let the cascade
cross regions. Reconnecting clients then flatten any unit that comes back,
and mail from outside piles up in the transport queue, ageing toward the
point where it is returned to the sender.

Source: Microsoft's preliminary Post Incident Report for EX1550543 /
MO1550821. Design notes: docs/plans/gateway-cascade-postmortem.md.
"""

import json
import time

from sregym.conductor.oracles.apps.workspace.gateway_recovery import WorkspaceRecoveryOracle, probes_meeting
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.workspace.application import KNOWN_GOOD_RELEASE, REGIONS, WorkspaceApplication
from sregym.utils.decorators import mark_fault_injected


class WorkspaceGatewayCascadeProblem(Problem):
    application_class = WorkspaceApplication
    run_default_workload = False
    cleanup_timeout_seconds = 1800
    recommended_agent_timeout_seconds = 3600

    def __init__(self, scale_tier="single"):
        super().__init__(self.application_class(scale_tier))
        self.kubectl = self.app.kubectl
        # The shared endpoint checks look at chat's Service.
        self.faulty_service, self.expected_service_port = "mattermost", 8065
        self.root_cause = self.build_structured_root_cause(
            component="statefulset/edge-gw-{a,b,c} (edge-gw releases 2026.07.2 and later)",
            namespace=self.namespace,
            description=(
                "A latent defect in the edge-gw gateway release line since 2026.07.2 (rolled out months ago, "
                "clean ever since): session admission goes through one shared, lock-protected session table "
                "whose per-admission work grows with the number of live sessions, and whose spinning waiters "
                "burn the CPU quota (worse with more worker threads). Past a load knee, units exhaust CPU and "
                "stop admitting connections, probes included. Two routine events pushed peak load past the "
                "knee: a gateway unit drained for maintenance, and the automated failover of chat from edge-alt "
                "onto the gateway fleet. The traffic manager's ejection and redirection (anomaly_redirect "
                "enabled, global spillover, no ejection cap) moved each failed unit's load onto the rest and "
                "across regions, so the collapse is fleet-wide and self-sustaining. Restarts, reverting the "
                "failover, restoring capacity or rolling back the latest release do not fix the defect; the "
                "fix is returning the fleet to the 2026.06 admission behaviour (rolling back to release "
                "2026.06.4, whose configuration lacks the later tuning keys) and returning units to service "
                "in controlled groups, while keeping queued mail from being returned to senders."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(self, self.root_cause)
        self.mitigation_oracle = WorkspaceRecoveryOracle(self)

    # ---- helpers -------------------------------------------------------------------

    def drained_units(self):
        """About a tenth of the fleet, as in the incident's ~14%."""
        units = self.app.tier["units"]
        drained = [f"edge-gw-b-{units - 1}"]
        if units * len(REGIONS) >= 15:
            drained.append(f"edge-gw-c-{units - 1}")
        return drained

    def collapse_state(self):
        app = self.app
        traffic = app.traffic_status()
        units = [u for u in traffic["units"].values() if u["state"] != "drained"]
        ejected = sum(1 for u in units if u["state"] == "ejected")
        status = app.client_status("60s")
        failing = [r for r in REGIONS if status["probes"].get(f"mail.{r}", {}).get("success_ratio", 0) < 0.5]
        return {"ejected": ejected, "units": len(units), "failing_regions": failing}

    def collapsed(self):
        state = self.collapse_state()
        self.observed = state
        return state["ejected"] >= 0.6 * state["units"] and len(state["failing_regions"]) >= 2

    # ---- lifecycle -----------------------------------------------------------------

    @mark_fault_injected
    def inject_fault(self):
        app = self.app
        app.set_population(app.population_for("baseline"))
        print("[1/5] Waiting for a healthy baseline at quiet-hours load", flush=True)
        app.wait_for(lambda: probes_meeting(app.client_status("60s"), 0.99, 20)[0], 900, 10, "healthy baseline")

        drained = self.drained_units()
        print(f"[2/5] Previous day: {', '.join(drained)} out of service for maintenance", flush=True)
        app.set_policy(drained=" ".join(drained))
        app.append_change(
            "edge-gw",
            f"{', '.join(drained)} out of service: NIC firmware fault, pending replacement. Headroom covers it.",
        )

        print("[3/5] Automated failover moves chat from edge-alt onto the shared gateway fleet", flush=True)
        app.set_policy(**{"placement.chat": "primary"})
        app.append_change("traffic-policy", "Automated failover: chat edge-alt -> primary (edge-alt probe anomaly).")
        time.sleep(60)

        print("[4/5] Load rises toward the daily peak", flush=True)
        baseline, peak = app.population_for("baseline"), app.population_for("peak")
        for step in (0.25, 0.5, 0.75, 1.0):
            app.set_population({k: int(baseline[k] + step * (peak[k] - baseline[k])) for k in peak})
            time.sleep(60)

        print("[5/5] Waiting for the cascade to become self-sustaining", flush=True)
        app.wait_for(self.collapsed, 900, 10, "fleet-wide collapse")
        time.sleep(90)
        if not self.collapsed():
            raise RuntimeError(f"Collapse did not persist: {self.observed}")
        print(f"Collapse established: {json.dumps(self.observed)}", flush=True)

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        """Reference repair, used when the agent's attempt did not pass."""
        app = self.app
        if app.command("get", "namespace", app.namespace, "--ignore-not-found", "-o", "name").strip():
            self.reference_repair()

    def reference_repair(self):
        app = self.app
        # 1. Keep queued mail from being returned while service is restored.
        site = app.command("get", "configmap", "mail-transport-site", "-o", "jsonpath={.data.site\\.cf}")
        site = "\n".join(
            line
            for line in site.splitlines()
            if not line.startswith(("maximal_queue_lifetime", "bounce_queue_lifetime"))
        )
        site += "\nmaximal_queue_lifetime = 5d\nbounce_queue_lifetime = 5d\n"
        app.command(
            "patch", "configmap", "mail-transport-site", "--type=merge", "-p", json.dumps({"data": {"site.cf": site}})
        )
        app.transport("postconf", "-e", "maximal_queue_lifetime = 5d", "bounce_queue_lifetime = 5d")
        app.transport("postfix", "reload")
        # 2. Stop the amplifiers and shed what can move: chat back to edge-alt.
        app.set_policy(
            anomaly_redirect="disabled",
            spillover="regional",
            max_ejection_percent=50,
            **{"placement.chat": "alternate"},
        )
        # 3. Roll back to the last release before the July change. Its
        #    configuration predates the tuning keys, so drop them first.
        config = app.command("get", "configmap", "edge-gw", "-o", "jsonpath={.data.edge-gw\\.conf}")
        config = "\n".join(
            line for line in config.splitlines() if not line.startswith(("worker_threads", "admission_timeout"))
        )
        app.command(
            "patch", "configmap", "edge-gw", "--type=merge", "-p", json.dumps({"data": {"edge-gw.conf": config + "\n"}})
        )
        version, image = KNOWN_GOOD_RELEASE
        for region in REGIONS:
            app.command("set", "image", f"statefulset/edge-gw-{region}", f"edge-gw={image}")
            app.command(
                "annotate",
                f"statefulset/edge-gw-{region}",
                f"kubernetes.io/change-cause=rollback to {version}",
                "--overwrite",
            )
            app.command("rollout", "status", f"statefulset/edge-gw-{region}", "--timeout=300s", timeout=330)
            app.wait_for(
                lambda r=region: all(
                    u["state"] in ("healthy", "drained")
                    for u in app.traffic_status()["units"].values()
                    if u["region"] == r
                ),
                300,
                10,
                f"region {region} units healthy",
            )
        # 4. Capacity back, then chat back onto the fleet, and flush the queue.
        app.set_policy(drained="", **{"placement.chat": "primary"})
        app.transport("postqueue", "-f")
