"""Does a confirmation that trails the action separate an agent?

Identical to `coordination_collapse` -- same fault, grader, budget and recovery
floor -- except that the store's compaction acknowledgement lags the compaction
by less than one stability window.

This is the only shape still standing after ten screens. A race beat the agent
twice in the notification family (`intermittent` 1 of 3, `delayed_audit` 0 of 2)
because the delivery record trailed the deliveries. This asks whether the same
property transfers to a different mechanism: a progress report that trails
progress, where acting on the stale report costs the window it was waiting for.
"""

from sregym.conductor.problems.coordination_collapse import CoordinationCollapse
from sregym.service.apps.coordination_lagging_ack import (
    CoordinationLaggingAck,
    CoordinationLaggingAdmission,
)


class CoordinationLaggingAckCollapse(CoordinationCollapse):
    """The compaction acknowledgement trails the compaction."""

    application_class = CoordinationLaggingAck


class CoordinationLaggingAdmissionCollapse(CoordinationCollapse):
    """The admitted fraction trails the admission.

    Built as a pair with the compaction variant because they differ in how often
    the stale value is met: compaction is one gate, admission is a repeated step.
    If only the repeated one bites, the property is about how many times the
    agent has to re-decide, not about the lag itself.
    """

    application_class = CoordinationLaggingAdmission
