"""A traffic surge saturates a provider network; capacity automation makes it an outage.

Modelled on Slack's outage of 2021-01-04
(https://slack.engineering/slacks-outage-on-january-4th-2021/). The only thing
injected is the surge: customers arrive faster than the provider's transit hop
was provisioned for. Everything after that is emergent. Connections waiting on
the hop block gateway workers, so gateway CPU falls while worker saturation
climbs; the saturation policy requests instances the provisioner cannot finish,
because each registration holds descriptors for as long as the hop takes and
the provisioner runs out of them; the stuck instances fill the policy maximum,
and the CPU policy keeps removing capacity in between.

The responder cannot change the hop. The provider restores its capacity only
after the responder submits, while the repair is being graded.

Scale tier 2 is two fleets behind the same provider network -- an nginx chat
edge and a push relay sending notifications to the push service -- that both
scale on the same two policies, register with the same provisioner, and share
the provider's instance quota, under which instances stuck provisioning hold
their place until they are gone. It states no capacity floor; chat served and
notifications delivered decide.
"""

import time

from sregym.conductor.oracles.apps.mattermost.transit_fleets_recovery import MattermostTransitFleetsOracle
from sregym.conductor.oracles.apps.mattermost.transit_recovery import MattermostTransitOracle, delta, served_ratio
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.mattermost.transit_cascade import MattermostTransitApplication, transit_application
from sregym.utils.decorators import mark_fault_injected

#: Reference repair: an open-files limit comfortably above a full fleet's registrations.
REPAIRED_OPEN_FILES = 4096
#: Tier 2's reference scaler repair: both fleets scale in on saturation and never
#: below two, and their maximums together fit the shared quota, with the push
#: relay's share large enough to deliver over the impaired network.
REPAIRED_SCALE_IN = {"min": 2, "scale_in": {"metric": "saturation", "below": 20}}
REPAIRED_FLEET_POLICY = {
    "chat-gateway": {**REPAIRED_SCALE_IN, "max": 16},
    "push-proxy": {**REPAIRED_SCALE_IN, "max": 4},
}
#: How long the surge may take to jam the provisioner. Measured on hp073: under 5
#: minutes from a fresh deploy, about 12.5 minutes after a reset to healthy.
CASCADE_DEADLINE_SECONDS = 1500


def cascade_formed(reached_max, open_files_errors, provisioning):
    """The fleet hit its maximum and the provisioner is out of descriptors with instances stuck behind it.

    Customers served short of the fleet's capacity is not enough on its own:
    the provisioner is one of the causes, so it has to be observable too.
    """
    return bool(reached_max and open_files_errors and provisioning)


def mitigation_oracle_class(scale_tier):
    """Tier 2 judges both fleets: chat served and notifications delivered."""
    return MattermostTransitFleetsOracle if str(scale_tier) == "2" else MattermostTransitOracle


class MattermostTransitSaturationProblem(Problem):
    application_class = MattermostTransitApplication
    #: Capacity restoration, provisioning and namespace teardown together.
    cleanup_timeout_seconds = 900

    def __init__(self, scale_tier="1"):
        super().__init__(transit_application(scale_tier))
        self.scale_tier = str(scale_tier)
        self.kubectl = self.app.kubectl
        self.faulty_service, self.expected_service_port = "chat-gateway", self.app.gateway_port
        self.surge_started = False
        self.observed = {}
        self.root_causes = self.fleet_root_causes() if self.scale_tier == "2" else self.gateway_root_causes()
        if self.app.instance_quota:
            self.root_causes.append(
                self.build_structured_root_cause(
                    component="cloud provider instance quota",
                    namespace=self.namespace,
                    description="chat-gateway and push-proxy share the provider's account-wide instance quota, and "
                    "it is exhausted: launches are refused with instance quota exceeded. Two things use it up, and "
                    "an account naming either one is correct: the two fleets' scaler maximums together exceed the "
                    "quota, so one fleet's growth takes places the other needs; and instances of both fleets stuck "
                    "provisioning, which never came into service, each hold their place for as long as they exist. "
                    "Raising either scaler maximum therefore adds no capacity, and fixing or restarting the "
                    "provisioner does not give the places back; only instances that are gone do.",
                )
            )
        self.root_cause = "\n".join(self.root_causes)
        self.diagnosis_oracle = LLMAsAJudgeOracle(self, self.root_causes)
        self.mitigation_oracle = mitigation_oracle_class(scale_tier)(self)
        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        app = self.app
        self.observed["healthy"] = {
            "fleet": app.gateway_fleet(),
            "customers": app.customer_stats(),
            "transit": app.transit_stats(),
            "policy": app.read_control("scaler.json"),
        }
        self.surge_started = True
        app.set_customer_rate(app.surge_rate)

        # Wait for the cascade to become observable on its own: the fleet's
        # requested size has reached the policy maximum, and the provisioner
        # has run out of descriptors with instances waiting on it.
        required = self.mitigation_oracle.required_degraded_ratio()
        reached_max = False
        before = app.customer_stats()
        deadline = time.monotonic() + CASCADE_DEADLINE_SECONDS
        while time.monotonic() < deadline:
            time.sleep(60)
            after = app.customer_stats()
            recent = served_ratio(delta(before, after))
            before = after
            fleet = app.gateway_fleet()
            reached_max = reached_max or fleet["requested"] >= app.fleet_max
            errors = app.provisioner_errors(since_seconds=60)
            if cascade_formed(reached_max, errors, fleet["provisioning"]):
                self.observed["cascade"] = {
                    "fleet": fleet,
                    "served_ratio_60s": round(recent, 3),
                    "required_served_ratio": round(required, 3),
                    "provisioner_open_files_errors_60s": errors,
                    "decisions": app.scaler_decisions()[-6:],
                    "transit": app.transit_stats(),
                }
                return
        raise RuntimeError(
            "The surge did not cascade into an outage at the policy maximum; "
            f"reached_max={reached_max} fleet={app.gateway_fleet()} decisions={app.scaler_decisions()[-3:]}"
        )

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        if not self.surge_started:
            return
        app = self.app
        # The provider's capacity increase, and the end of the surge.
        app.set_transit_capacity(app.transit_capacity_fixed)
        app.set_customer_rate(app.healthy_rate)
        # Responder's half: let registrations through, keep capacity at its floor.
        self.raise_provisioner_open_files(REPAIRED_OPEN_FILES)
        self.repair_scaler_policy()
        app.command("rollout", "status", "deployment/provisioner", "--timeout=300s", timeout=330)
        if app.instance_quota and (stuck := app.instances_provisioning()):
            # Stuck instances hold places under the quota; replacing them frees those places.
            app.command("delete", "pod", *stuck, "--ignore-not-found", "--wait=false")
        for name in app.capacity_groups:
            app.command("rollout", "status", f"deployment/{name}", "--timeout=600s", timeout=630)
        app.wait_for_gateway()
        self.surge_started = False

    def gateway_root_causes(self):
        return [
            self.build_structured_root_cause(
                component="provider-managed network between chat-gateway and its backend",
                namespace=self.namespace,
                description="Under the traffic surge, the provider-managed network path from chat-gateway (and the "
                "provisioner) to the chat backend drops and delays connections: requests crossing it stall and are "
                "reset. The degradation is in that external network path, not in a component inside the cluster, "
                "and the responder cannot change it.",
            ),
            self.build_structured_root_cause(
                component="capacity-scaler",
                namespace=self.namespace,
                description="The capacity-scaler's scale-in policy is keyed on CPU. Gateway workers blocked on the "
                "slow network use little CPU, so the policy reads a saturated gateway as idle and removes replicas, "
                "fighting the saturation-keyed scale-out policy and holding capacity below the service's floor. CPU "
                "is the wrong scale-in signal for a pool blocked on I/O.",
            ),
            self.build_structured_root_cause(
                component="provisioner",
                namespace=self.namespace,
                description="New chat-gateway instances must register with the provisioner before serving. Each "
                "registration holds file descriptors while it fetches configuration across the slow network, and a "
                "registration that fails partway never releases its lease file, so descriptors accumulate. The "
                "provisioner's low open-files limit (ulimit -n) is exhausted (Too many open files) and stays "
                "exhausted after the network recovers, until the provisioner is restarted or its limit raised; new "
                "instances stay stuck in initialization and fill the scaler's maximum replica count without adding "
                "capacity.",
            ),
        ]

    def fleet_root_causes(self):
        """Tier 2: the same three causes, across both fleets."""
        return [
            self.build_structured_root_cause(
                component="provider-managed network between the fleets and what they call",
                namespace=self.namespace,
                description="Under the traffic surge, the provider-managed network path that chat-gateway (and the "
                "provisioner) use to reach the chat backend, and that push-proxy uses to reach the push service, "
                "drops and delays connections: requests crossing it stall and are reset. The degradation is in that "
                "external network path, not in a component inside the cluster, and the responder cannot change it.",
            ),
            self.build_structured_root_cause(
                component="capacity-scaler",
                namespace=self.namespace,
                description="The capacity-scaler's scale-in policies for both chat-gateway and push-proxy are keyed "
                "on CPU. Instances blocked on the slow network use little CPU, so each policy reads a saturated "
                "fleet as idle and removes instances, fighting the saturation-keyed scale-out policy of the same "
                "fleet. CPU is the wrong scale-in signal for fleets blocked on I/O.",
            ),
            self.build_structured_root_cause(
                component="provisioner",
                namespace=self.namespace,
                description="New chat-gateway and push-proxy instances must both register with the provisioner "
                "before serving. Each registration holds file descriptors while it fetches configuration across the "
                "slow network, and a registration that fails partway never releases its lease file, so descriptors "
                "accumulate. The provisioner's low open-files limit (ulimit -n) is exhausted (Too many open files) "
                "and stays exhausted after the network recovers, until the provisioner is restarted or its limit "
                "raised; new instances of both fleets stay stuck in initialization.",
            ),
        ]

    def repair_scaler_policy(self):
        """Tier 1 keeps its stated floor; tier 2 stops reading either fleet's blocked instances as idle."""
        if self.scale_tier != "2":
            self.app.scaler_policy(min=self.app.capacity_floor)
            return
        for group in self.app.capacity_groups:
            self.app.scaler_policy(group, **REPAIRED_FLEET_POLICY[group])

    def raise_provisioner_open_files(self, limit):
        self.app.raise_open_files(limit)


# Existing class names remain available to callers.
MattermostTransitSaturation = MattermostTransitSaturationProblem
