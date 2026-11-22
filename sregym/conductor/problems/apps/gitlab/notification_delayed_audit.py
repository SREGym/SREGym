"""Recovery must wait for independent delivery evidence to become complete."""

from sregym.conductor.problems.apps.gitlab.notification_intermittent import GitLabNotificationIntermittentProblem
from sregym.service.apps.gitlab.notifications import (
    GitLabNotificationDelayedAuditApplication as DelayedAuditApplication,
)


class GitLabNotificationDelayedAuditProblem(GitLabNotificationIntermittentProblem):
    application_class = DelayedAuditApplication
    root_cause_description = (
        "Wrong-primary deletion propagated to PostgreSQL replicas while durable Sidekiq jobs "
        "retained obsolete issue IDs. SMTP failures continue during recovery, and the independent provider "
        "audit publishes acceptance receipts late. Restore acknowledged data and reconcile each original "
        "notification using complete provider evidence before retrying uncertain sends."
    )


# Existing class names remain available to callers.
GitLabNotificationDelayedAudit = GitLabNotificationDelayedAuditProblem
