"""Native problem cases used by the imported-contract and chart tests."""

from sregym.conductor.problems.apps.frappe.desk_and_queue_outage import FrappeDeskAndQueueOutage
from sregym.conductor.problems.apps.frappe.writes_and_queue_oom import FrappeWritesAndQueueOOM
from sregym.conductor.problems.apps.saleor.statement_timeout import SaleorCheckoutStatementTimeoutCanary
from sregym.conductor.problems.apps.slack_spine.lock_leaks import SlackDistractorVolumeSeqLock, SlackSeqLockLeak
from sregym.conductor.problems.apps.slack_spine.maintenance import SlackMaintenanceCollision
from sregym.conductor.problems.apps.slack_spine.sequencer import SlackSplitSequencer
from sregym.conductor.problems.apps.slack_spine.store_consistency import SlackLoginsUnreadSendsAllSlow
from sregym.conductor.problems.apps.slack_spine.strict_mode import (
    SlackSendsFailComplianceWindow,
    SlackSendsFailStrictMode,
)

# In Incident Arena task order.
INCIDENT_ARENA_PROBLEM_CLASSES = (
    FrappeDeskAndQueueOutage,
    FrappeWritesAndQueueOOM,
    SaleorCheckoutStatementTimeoutCanary,
    SlackSplitSequencer,
    SlackMaintenanceCollision,
    SlackLoginsUnreadSendsAllSlow,
    SlackSendsFailStrictMode,
    SlackSendsFailComplianceWindow,
    SlackSeqLockLeak,
    SlackDistractorVolumeSeqLock,
)

INCIDENT_ARENA_PROBLEMS = {cls.PROBLEM_ID: cls for cls in INCIDENT_ARENA_PROBLEM_CLASSES}
