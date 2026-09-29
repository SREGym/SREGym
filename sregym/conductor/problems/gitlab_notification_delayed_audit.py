"""Recovery must wait for independent delivery evidence to become complete."""

from sregym.conductor.problems.gitlab_notification_intermittent import GitLabNotificationIntermittent
from sregym.service.apps.gitlab_notification_delayed_audit import (
    GitLabNotificationDelayedAudit as DelayedAuditApplication,
)


class GitLabNotificationDelayedAudit(GitLabNotificationIntermittent):
    application_class = DelayedAuditApplication

    def __init__(self, scale_tier="replicated"):
        super().__init__(scale_tier)
        self.root_cause = self.build_structured_root_cause(
            component="gitlab-ce-db",
            namespace=self.namespace,
            description="Wrong-primary deletion propagated to PostgreSQL replicas while durable Sidekiq jobs "
            "retained obsolete issue IDs. SMTP failures continue during recovery, and the independent provider "
            "audit publishes acceptance receipts late. Restore acknowledged data and reconcile each original "
            "notification using complete provider evidence before retrying uncertain sends.",
        )
        self.diagnosis_oracle.expected = self.root_cause

    def prepare_recovery_material(self):
        super().prepare_recovery_material()
        self.app.archive_command(
            "sh",
            "-c",
            "printf '\\nProvider update: receipt publication is delayed during the continuing SMTP incident. "
            "See the completeness-watermark contract in /recovery/provider-audit.txt.\\n' "
            ">> /recovery/notification-operations.log",
        )
