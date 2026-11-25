"""New nodes joined the ring holding nothing and answered for data they did not have.

After Monzo's incident of 29 July 2019 (its write-up, quoted below). Monzo
added six servers to a Cassandra cluster of 21 with `auto_bootstrap` set to
false, believing they would "join the cluster, and stay inactive until we
carried out a further action". Instead "they immediately took part in the
reading and writing of data, despite not actually holding any data": where two
or three of a row's three replicas were now new servers, a quorum read found
nothing. The scale-up was discounted at first "as everything appears healthy".
Monzo took the servers out "one-by-one, to allow us to return data ownership
safely", and then spent the rest of the day on "all the data that had been
written while the six new servers were actively serving reads and writes".

Here the ring has three nodes and the on-call adds three. The tokens are
planned so that a fifth of the ring has all three of its copies on new nodes
(those customers see nothing at all, and nothing heals it) and almost a third
has two (those customers see nothing now and then, until a read repairs it).

What is ours, not the write-up's: customers who cannot see what they set up set
it up again. That is what makes the hours after 15:08 here.

The properties this problem tests:

- The quick ways out report success and lose data. Deleting the new nodes and
  telling the ring to forget them loses what customers wrote while those nodes
  were the only replicas (in a test, 109 of 400 writes). Keeping them and
  running repair "completed successfully" brings nothing back, because no
  current replica holds the old rows, and a cleanup of the old nodes then
  deletes the only copies. What works is what Monzo did: decommission each new
  node, so it hands what it holds to the nodes that take its ranges over.
- The capacity is still wanted, so the nodes have to go back in properly: the
  setting changed, their disks cleared (a decommissioned node will not rejoin
  with its old one), and one at a time (the StatefulSet starts pods together,
  and Cassandra refuses two bootstraps at once).
- Once the old data can be read again, every customer who set something up
  again has it twice: two payees with the same account, two standing orders
  paying it the same amount, both being paid. Nothing marks them; they are
  ordinary rows made by the customer through the app. The note says what
  customers are owed ("exactly as they left it", "paid when it is due, once");
  it does not say that customers have been setting things up again. In some
  runs a customer or two already has something twice at the handover, where a
  read repaired the old row after they had acted (0 to 3 in the runs so far).
- Customers' own changes since have to survive: payees and standing orders
  they added, and standing orders they cancelled.
"""

import time
from datetime import UTC, datetime

from sregym.conductor.oracles.apps.larkbank.ring_recovery import RingRecoveryOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.larkbank.application import (
    CHANGE_CAUSE,
    NODES,
    NODES_WANTED,
    RING,
    RING_CONFIG,
    LarkbankApplication,
)
from sregym.utils.decorators import mark_fault_injected


def clock():
    return datetime.now(UTC).strftime("%H:%M")


class LarkbankEmptyNodesProblem(Problem):
    application_class = LarkbankApplication
    run_default_workload = False
    cleanup_timeout_seconds = 900
    recommended_agent_timeout_seconds = 1800

    def __init__(self, scale_tier="small"):
        super().__init__(self.application_class(scale_tier))
        self.kubectl = self.app.kubectl
        self.faulty_service = RING
        #: The world's clock when the agent took over.
        self.handover_at = None
        self.root_cause = self.build_structured_root_cause(
            component=RING,
            namespace=self.namespace,
            description=f"The ring's settings (ConfigMap {RING_CONFIG}, overrides.yaml, added to cassandra.yaml when a "
            f"node starts) set auto_bootstrap: false. When the on-call scaled the StatefulSet {RING} from {NODES} to "
            f"{NODES_WANTED}, the three new Cassandra nodes ({RING}-3, {RING}-4, {RING}-5) therefore joined the ring "
            "at their planned tokens at once, as normal members, without first streaming the data for the ranges "
            "they took over. Rows are read and written at QUORUM with three replicas. For the part of the ring whose "
            "three replicas are now all new nodes, every read finds nothing; where two of the three are new, a read "
            "finds nothing whenever the two new ones answer, until a read repair copies the row over. The old nodes "
            "still hold all the old data but are no longer replicas for those ranges. So customers' payees and "
            "standing orders vanished from the app and the scheduler did not pay the standing orders it could not "
            "read, while nodetool status shows all six nodes up and normal.",
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = RingRecoveryOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        app, tier = self.app, self.app.tier
        print(f"[1/4] Customers use the app for {tier['steady_seconds']}s; standing orders are paid")
        time.sleep(tier["steady_seconds"])
        before = app.world_get("/summary")
        if before["payments"] < 100 or before["did"]:
            raise RuntimeError(f"the bank is not working as it should before the incident: {before}")
        print(f"[2/4] The on-call adds three nodes to the ring ({CHANGE_CAUSE})")
        added = clock()
        app.world_post("/mark?name=scale")
        app.scale_ring(NODES_WANTED, CHANGE_CAUSE)
        app.wait_ring(NODES_WANTED, 600)
        print(f"[3/4] {tier['wild_seconds']}s of customers finding things gone")
        time.sleep(tier["wild_seconds"])
        print("[4/4] Handover")
        summary = app.world_get("/summary")
        print(f"      {summary}")
        if summary["did"].get("set a standing order up again", 0) < 15:
            # With too few, the run would test something else.
            raise RuntimeError(f"the incident did not happen as written: {summary}")
        self.handover_at = app.world_post("/mark?name=handover")["now"]
        app.description = app.handover_description(added)

    @mark_fault_injected
    def recover_fault(self):
        """Put customers right with the reference repair, the app still up.

        What was lost is sight of the data, and what customers did meanwhile has to be kept, so recovering
        is repairing: each new node decommissioned in turn (it hands what it holds to the nodes that take
        its ranges over), what customers set up twice removed as it comes into view, the setting changed
        so that a new node takes its data before it serves, and the three nodes added again one at a time.

        This must not raise. It also runs when an attempt is cleaned up, and a failed cleanup marks the
        attempt incomplete.
        """
        if self.handover_at is None:
            return
        try:
            app = self.app
            if app.nodes_running() > NODES and app.new_nodes_started_empty():
                app.remove_new_nodes(between=lambda: print(f"reference repair: {app.dedupe()}"))
            app.fix_setting()
            time.sleep(10)  # customers look again and see what they have twice
            print(f"reference repair: {app.dedupe()}")
            app.add_nodes()
            print(f"reference repair: {app.dedupe()}")
        except Exception as error:  # noqa: BLE001 - see above
            print(f"reference repair did not run to the end: {error}")
