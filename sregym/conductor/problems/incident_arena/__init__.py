"""Problems ported from Incident Arena (https://github.com/abundant-ai/incident-arena).

Registry ids, in Incident Arena task order (000-019), are listed in
``INCIDENT_ARENA_PROBLEMS``; see docs/incident-arena.md for the mapping.
"""

from sregym.conductor.problems.incident_arena.frappe import (
    FrappeDeletesAndJobsFail,
    FrappeDeskAndQueueOOM,
    FrappeDeskAndQueueOutage,
    FrappeNewRecordsAndJobsFail,
    FrappeNewRecordsAndQueueOOM,
    FrappeWritesAndQueueOOM,
)
from sregym.conductor.problems.incident_arena.saleor import SaleorCheckoutStatementTimeoutCanary
from sregym.conductor.problems.incident_arena.slack_spine import (
    SlackDistractorVolumeSeqLock,
    SlackLoginsUnreadSendsAllSlow,
    SlackLoginsUnreadSendsSlower,
    SlackMaintenanceCollision,
    SlackSendsCrawlThenStoreSlows,
    SlackSendsFailComplianceWindow,
    SlackSendsFailStrictMode,
    SlackSendsFailStrictModePlausiblePool,
    SlackSendsFailStrictPool16,
    SlackSendsSlowAndStallEveryMinute,
    SlackSeqLockLeak,
    SlackSplitSequencer,
    SlackStallEveryMinuteThenCrawl,
)

# Incident Arena task order 000-019.
INCIDENT_ARENA_PROBLEM_CLASSES = (
    FrappeDeletesAndJobsFail,
    FrappeDeskAndQueueOOM,
    FrappeDeskAndQueueOutage,
    FrappeNewRecordsAndJobsFail,
    FrappeNewRecordsAndQueueOOM,
    FrappeWritesAndQueueOOM,
    SaleorCheckoutStatementTimeoutCanary,
    SlackSplitSequencer,
    SlackMaintenanceCollision,
    SlackLoginsUnreadSendsAllSlow,
    SlackLoginsUnreadSendsSlower,
    SlackSendsCrawlThenStoreSlows,
    SlackSendsFailStrictModePlausiblePool,
    SlackSendsFailStrictMode,
    SlackSendsFailComplianceWindow,
    SlackSendsFailStrictPool16,
    SlackSendsSlowAndStallEveryMinute,
    SlackStallEveryMinuteThenCrawl,
    SlackSeqLockLeak,
    SlackDistractorVolumeSeqLock,
)

INCIDENT_ARENA_PROBLEMS = {cls.PROBLEM_ID: cls for cls in INCIDENT_ARENA_PROBLEM_CLASSES}

__all__ = ["INCIDENT_ARENA_PROBLEMS", "INCIDENT_ARENA_PROBLEM_CLASSES"]
