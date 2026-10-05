"""Problems ported from Incident Arena (https://github.com/abundant-ai/incident-arena).

Incident Arena builds many of its tasks by combining independent faults. A
combination is ported only when it covers a fault no other ported problem
has; see docs/incident-arena.md for the mapping.
"""

from sregym.conductor.problems.incident_arena.frappe import FrappeDeskAndQueueOutage, FrappeWritesAndQueueOOM
from sregym.conductor.problems.incident_arena.saleor import SaleorCheckoutStatementTimeoutCanary
from sregym.conductor.problems.incident_arena.slack_spine import (
    SlackDistractorVolumeSeqLock,
    SlackLoginsUnreadSendsAllSlow,
    SlackLoginsUnreadSendsSlower,
    SlackMaintenanceCollision,
    SlackSendsFailComplianceWindow,
    SlackSendsFailStrictMode,
    SlackSeqLockLeak,
    SlackSplitSequencer,
)

# In Incident Arena task order.
INCIDENT_ARENA_PROBLEM_CLASSES = (
    FrappeDeskAndQueueOutage,
    FrappeWritesAndQueueOOM,
    SaleorCheckoutStatementTimeoutCanary,
    SlackSplitSequencer,
    SlackMaintenanceCollision,
    SlackLoginsUnreadSendsAllSlow,
    SlackLoginsUnreadSendsSlower,
    SlackSendsFailStrictMode,
    SlackSendsFailComplianceWindow,
    SlackSeqLockLeak,
    SlackDistractorVolumeSeqLock,
)

INCIDENT_ARENA_PROBLEMS = {cls.PROBLEM_ID: cls for cls in INCIDENT_ARENA_PROBLEM_CLASSES}

__all__ = ["INCIDENT_ARENA_PROBLEMS", "INCIDENT_ARENA_PROBLEM_CLASSES"]
