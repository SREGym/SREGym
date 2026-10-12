"""Coordination services whose status reports lag completed operations.

Compaction and admission acknowledgements arrive before the next safe operation
window. The event ledger records operations immediately.
"""

from sregym.service.apps.coordination.application import CoordinationApplication

#: Seconds the reported compaction state trails the real one, by tier. Both are
#: well under their tier's stability window (60s and 90s).
ACK_LAG = {"single": 40, "replicated": 55}

#: Seconds the reported admitted fraction trails the real one. Shorter than the
#: admission step interval (45s and 60s), so a responder who respects the step
#: interval never still sees a stale value when the next step becomes allowed.
ADMISSION_ACK_LAG = {"single": 30, "replicated": 40}


class LaggingAckApplication(CoordinationApplication):
    """Configure one or more acknowledgement lags on the coordinator."""

    #: Map each acknowledgement setting to its delay in seconds for each tier.
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


class CoordinationLaggingAckApplication(LaggingAckApplication):
    """The compaction acknowledgement trails the compaction."""

    LAGS = {"COMPACTION_ACK_LAG_SECONDS": ACK_LAG}


class CoordinationLaggingAdmissionApplication(LaggingAckApplication):
    """The reported admitted fraction trails each admission step."""

    LAGS = {"ADMISSION_ACK_LAG_SECONDS": ADMISSION_ACK_LAG}


# Existing class names remain available to callers.
LaggingAck = LaggingAckApplication
CoordinationLaggingAck = CoordinationLaggingAckApplication
CoordinationLaggingAdmission = CoordinationLaggingAdmissionApplication
