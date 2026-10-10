"""Coordination collapse with delayed compaction or admission acknowledgements."""

from sregym.conductor.problems.coordination_collapse import CoordinationCollapse
from sregym.service.apps.coordination.lagging_acknowledgement import (
    CoordinationLaggingAck,
    CoordinationLaggingAdmission,
)


class CoordinationLaggingAckCollapse(CoordinationCollapse):
    """The compaction acknowledgement trails the compaction."""

    application_class = CoordinationLaggingAck


class CoordinationLaggingAdmissionCollapse(CoordinationCollapse):
    """The reported admitted fraction trails each admission step."""

    application_class = CoordinationLaggingAdmission
