"""Coordination collapse with delayed compaction or admission acknowledgements."""

from sregym.conductor.problems.coordination.collapse import CoordinationCollapseProblem
from sregym.service.apps.coordination.lagging_acknowledgement import (
    CoordinationLaggingAckApplication,
    CoordinationLaggingAdmissionApplication,
)


class CoordinationLaggingAckCollapseProblem(CoordinationCollapseProblem):
    """The compaction acknowledgement trails the compaction."""

    application_class = CoordinationLaggingAckApplication


class CoordinationLaggingAdmissionCollapseProblem(CoordinationCollapseProblem):
    """The reported admitted fraction trails each admission step."""

    application_class = CoordinationLaggingAdmissionApplication


# Existing class names remain available to callers.
CoordinationLaggingAckCollapse = CoordinationLaggingAckCollapseProblem
CoordinationLaggingAdmissionCollapse = CoordinationLaggingAdmissionCollapseProblem
