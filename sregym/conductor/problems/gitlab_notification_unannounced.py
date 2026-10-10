"""Notification recovery faults without a mail subsystem hint in the app description.

The fault mechanisms and recovery checks match their announced variants.
"""

from sregym.conductor.problems.gitlab_notification_ambiguity import GitLabNotificationAmbiguity
from sregym.conductor.problems.gitlab_notification_recovery import GitLabNotificationRecovery
from sregym.service.apps.gitlab.notifications import (
    GitLabNotificationUnannounced as UnannouncedApplication,
)
from sregym.service.apps.gitlab.notifications import (
    GitLabNotificationUnannouncedAmbiguity as UnannouncedAmbiguityApplication,
)


class GitLabNotificationUnannounced(GitLabNotificationRecovery):
    application_class = UnannouncedApplication


class GitLabNotificationUnannouncedAmbiguity(GitLabNotificationAmbiguity):
    application_class = UnannouncedAmbiguityApplication
