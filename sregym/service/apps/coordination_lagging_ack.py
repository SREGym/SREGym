"""The coordination cluster with a confirmation that trails reality.

`coordination_collapse` was built to be hard -- a 360-second recovery floor, five
gated phases, premature action regressing progress, a status endpoint that lies
while churning, permanently accumulating loss -- and was solved 3 of 3 with every
protective mechanic untouched. The agent read each refusal and waited.

The refusals are why. Every one of them is honest and prescriptive:

    leader stable for 12s; 60s required. This attempt restarted the stability
    window.

Screening across ten problems narrowed what actually separates an agent to one
property: **verification cannot establish correctness.** Tooling that lies about
*state* is not enough, because waiting resolves it. What is not resolved by
waiting is a confirmation that trails the action -- the agent compacts, the store
really does compact, and the reported state still says it has not.

So this variant changes exactly one thing: `COMPACTION_ACK_LAG_SECONDS`. A
compaction that has succeeded keeps reporting `compacted: false` for that long,
and compacting again inside the window is a second write storm -- it adds debt
and restarts the stability window, which is the existing penalty for haste.

The lag is shorter than the stability window the agent must already wait out, so
patience is always sufficient and the task stays solvable. The event ledger
records `compacted` the instant it happens, so an agent that cross-checks its own
actions against the ledger rather than trusting the status reads through it.
"""

from sregym.service.apps.coordination_cluster import CoordinationCluster

#: Seconds the reported compaction state trails the real one, by tier. Both are
#: well under their tier's stability window (60s and 90s).
ACK_LAG = {"single": 40, "replicated": 55}

#: Seconds the reported admitted fraction trails the real one. Shorter than the
#: admission step interval (45s and 60s), so a responder who respects the step
#: interval never still sees a stale value when the next step becomes allowed.
ADMISSION_ACK_LAG = {"single": 30, "replicated": 40}


class LaggingAck(CoordinationCluster):
    """Configure one or more acknowledgement lags on the coordinator."""

    #: Environment variable name to per-tier seconds. Empty means this class
    #: would be identical to its parent, which `application_documents` refuses:
    #: a variant that silently changes nothing would look like an experiment and
    #: measure nothing.
    LAGS: dict[str, dict[str, int]] = {}

    def lag_env(self):
        return [{"name": name, "value": str(table[self.scale_tier])} for name, table in self.LAGS.items()]

    def application_documents(self):
        if not self.LAGS:
            raise RuntimeError(f"{type(self).__name__} declares no lag; it would be identical to its parent")
        documents = super().application_documents()
        patched = 0
        for document in documents:
            if document.get("kind") != "Deployment":
                continue
            for container in document["spec"]["template"]["spec"]["containers"]:
                if container["name"] != "coordinator":
                    continue
                container["env"].extend(self.lag_env())
                patched += 1
        if patched != 1:
            raise RuntimeError(f"Expected exactly one coordinator container to configure, patched {patched}")
        return documents


class CoordinationLaggingAck(LaggingAck):
    """The compaction acknowledgement trails the compaction."""

    LAGS = {"COMPACTION_ACK_LAG_SECONDS": ACK_LAG}


class CoordinationLaggingAdmission(LaggingAck):
    """The admitted fraction trails the admission.

    A different mechanism in the same family. Compaction is a one-shot gate, so
    its lag is survived by waiting once; admission is a repeated step, so a
    responder ramping traffic meets the stale value on every step and has to
    decide each time whether its last action took effect.
    """

    LAGS = {"ADMISSION_ACK_LAG_SECONDS": ADMISSION_ACK_LAG}
