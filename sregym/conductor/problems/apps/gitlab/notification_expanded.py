"""A larger multi-tenant recovery tail under the same fault and outcome contract."""

from sregym.conductor.problems.apps.gitlab.notification_delayed_audit import GitLabNotificationDelayedAuditProblem
from sregym.service.apps.gitlab.notifications import GitLabNotificationExpandedApplication as ExpandedApplication


class GitLabNotificationExpandedProblem(GitLabNotificationDelayedAuditProblem):
    application_class = ExpandedApplication
    audit_noise_per_batch = 800


# Existing class names remain available to callers.
GitLabNotificationExpanded = GitLabNotificationExpandedProblem
