"""A larger multi-tenant recovery tail under the same fault and outcome contract."""

from sregym.conductor.problems.gitlab_notification_delayed_audit import GitLabNotificationDelayedAudit
from sregym.service.apps.gitlab_notification_expanded import GitLabNotificationExpanded as ExpandedApplication


class GitLabNotificationExpanded(GitLabNotificationDelayedAudit):
    application_class = ExpandedApplication
    audit_noise_per_batch = 800
