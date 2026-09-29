"""An ongoing SMTP impairment makes notification recovery itself stateful."""

from sregym.conductor.oracles.gitlab_notification_recovery import notification_report
from sregym.conductor.problems.gitlab_notification_ambiguity import GitLabNotificationAmbiguity
from sregym.service.apps.gitlab_notification_intermittent import (
    GitLabNotificationIntermittent as IntermittentApplication,
)


class GitLabNotificationIntermittent(GitLabNotificationAmbiguity):
    application_class = IntermittentApplication

    def __init__(self, scale_tier="replicated"):
        super().__init__(scale_tier)
        self.reference_delivery_history = []
        self.root_cause = self.build_structured_root_cause(
            component="gitlab-ce-db",
            namespace=self.namespace,
            description="Wrong-primary deletion reached every PostgreSQL replica, while independently durable "
            "Sidekiq jobs refer to pre-restore issue IDs. SMTP failures remain intermittent during recovery: "
            "some failed sends are already accepted, others are not. Preserve acknowledged data, reconcile "
            "each delivery against the provider audit, and prevent automatic retries from duplicating mail.",
        )
        self.diagnosis_oracle.expected = self.root_cause

    def prepare_recovery_material(self):
        super().prepare_recovery_material()
        self.app.ongoing_fault(True)
        self.app.archive_write(
            "/recovery/notification-operations.log",
            "Earlier audit: six incident notifications accepted before the worker was resumed.\n"
            "Subsequent worker batch: SMTP transport failures occurred during DATA; the sender could not "
            "confirm which attempts were accepted. Redis retains failed work.\n"
            "Current provider advisory: intermittent DATA connection failures continue during recovery. "
            "A failed send may already be recipient-visible, or may not have been accepted. "
            "Successful and failed attempts are interleaved. The paginated provider audit remains available.\n"
            "Automatic retries can duplicate accepted mail. Reconcile the original intent without duplicate "
            "or wrong-recipient deliveries while restoring normal GitLab notification service.\n"
            "Issue sequence allocations included aborted import reservations after the staging archive.\n",
        )

    def reconcile_notifications(self):
        self.app.control("stop", "sidekiq")
        self.reference_delivery_result = self.app.deliver_reconciled_notifications(self.notifications)
        self.reference_delivery_history.append(self.reference_delivery_result)
        report = notification_report(self.notifications, self.app.public_messages())
        self.app.control("start", "sidekiq")
        return report
