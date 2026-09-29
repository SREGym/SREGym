"""Database rollback with an independently durable, partially delivered mail queue.

Synthetic extension of GitLab's 2017 recovery risks: restoring public issue URLs
must not replay Redis jobs against different objects that reuse internal IDs.
"""

import json
import time

from sregym.conductor.oracles.gitlab_database_recovery import GitLabDatabaseRecoveryOracle
from sregym.conductor.oracles.gitlab_notification_recovery import (
    GitLabNotificationRecoveryOracle,
    notification_report,
)
from sregym.conductor.problems.gitlab_database_deletion import GitLabDatabaseDeletion
from sregym.service.apps.gitlab_notification_recovery import (
    NOTIFICATION_JOURNAL,
    NOTIFICATION_REASON,
)
from sregym.service.apps.gitlab_notification_recovery import (
    GitLabNotificationRecovery as GitLabNotificationApplication,
)
from sregym.utils.decorators import mark_fault_injected


class GitLabNotificationRecovery(GitLabDatabaseDeletion):
    application_class = GitLabNotificationApplication
    # A logical GitLab restore, Rails boot and queue reconciliation may exceed
    # the generic five-minute cleanup drain. This does not extend agent time.
    cleanup_timeout_seconds = 900

    def __init__(self, scale_tier="replicated"):
        super().__init__(scale_tier)
        self.notifications = []
        self.mitigation_oracle = GitLabNotificationRecoveryOracle(self)
        self.root_cause = self.build_structured_root_cause(
            component="gitlab-ce-db",
            namespace=self.namespace,
            description="Wrong-primary schema deletion reached all physical replicas. The usable staging backup "
            "predates acknowledged issues and Redis notification jobs. Post-snapshot sequence gaps mean public-IID "
            "replay can reuse internal issue IDs for different objects. Some notifications have already reached "
            "the independent SMTP provider; reconcile remaining intent without wrong or duplicate deliveries.",
        )
        self.diagnosis_oracle.expected = self.root_cause

    def before_acknowledged_writes(self):
        # Normal aborted transactions/import reservations consume sequence values;
        # they are not represented in the earlier logical archive.
        self.app.sql("SELECT nextval(pg_get_serial_sequence('issues','id')) FROM generate_series(1,11);")

    def prepare_recovery_material(self):
        self.app.control("stop", "sidekiq")
        super().prepare_recovery_material()
        records = json.dumps(self.receipts)
        self.notifications = self.app.rails(f"""
records = JSON.parse({json.dumps(records)})
result = records.each_with_index.map do |r, index|
  issue = Issue.find_by!(project_id: r.fetch('project'), iid: r.fetch('iid'))
  recipient = User.find_by_username!("incident-user-#{{{json.dumps(self.app.projects)}.index(r.fetch('project')) + 1}}")
  mail = Notify.new_issue_email(recipient.id, issue.id, {json.dumps(NOTIFICATION_REASON)})
  job = index < 6 ? (mail.deliver_now; nil) : mail.deliver_later
  r.merge('recipient' => recipient.email, 'recipient_id' => recipient.id,
          'issue_id_at_acceptance' => issue.id, 'job_id' => job&.job_id,
          'provider_job_id' => job&.provider_job_id, 'reason' => {json.dumps(NOTIFICATION_REASON)})
end
puts 'RESULT:' + result.to_json
""")
        self.app.archive_write(NOTIFICATION_JOURNAL, json.dumps(self.notifications, indent=2) + "\n")
        report = notification_report(self.notifications, self.app.messages())
        if (
            report["delivered"] != 6
            or report["missing"] != len(self.notifications) - 6
            or report["wrong_deliveries"]
            or report["duplicates"]
        ):
            raise RuntimeError(f"Notification fixture did not establish six accepted deliveries: {report}")
        self.app.archive_write(
            "/recovery/notification-operations.log",
            "Provider audit confirms six deliveries before the database incident. Remaining acknowledged mail "
            "was queued in Redis. Issue sequence allocations included aborted import reservations after staging.\n",
        )

    @mark_fault_injected
    def inject_fault(self):
        super().inject_fault()
        # Resume the real worker against the lost database, allowing normal retry
        # behavior. Recovery must coordinate its independent persistent queue.
        self.app.control("start", "sidekiq")

    def reconcile_notifications(self):
        app = self.app
        app.control("stop", "sidekiq")
        messages = app.messages()
        missing = [r for r in self.notifications if notification_report([r], messages)["missing"]]
        app.rebind_notifications(missing)
        app.control("start", "sidekiq")
        deadline = time.monotonic() + 300
        while True:
            report = notification_report(self.notifications, app.messages())
            if report["missing"] == 0:
                return report
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Reference notification recovery did not drain: {report}")
            time.sleep(2)

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        if self.deletion_attempted:
            # Cleanup can repair availability but cannot undo mail already sent.
            # Preserve that evidence; a valid failed attempt must still tear down.
            self.app.control("stop", "sidekiq")
            if not GitLabDatabaseRecoveryOracle.evaluate(self.mitigation_oracle).get("success"):
                self.app.restore_archive()
                self.app.recovery_client("replay", receipts=self.receipts)
            self.reconcile_notifications()
        self.deletion_attempted = False
