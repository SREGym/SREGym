"""Notification recovery faults without a mail subsystem hint in the app description.

The fault mechanisms and recovery checks match their announced variants.
"""

from functools import partial

from sregym.conductor.problems.apps.gitlab.notification_ambiguity import GitLabNotificationAmbiguityProblem
from sregym.conductor.problems.apps.gitlab.notification_recovery import GitLabNotificationRecoveryProblem
from sregym.service.apps.gitlab.notifications import (
    UNANNOUNCED_AMBIGUOUS_NOTIFICATIONS,
    UNANNOUNCED_NOTIFICATIONS,
    GitLabNotificationApplication,
)


class GitLabNotificationUnannouncedProblem(GitLabNotificationRecoveryProblem):
    application_class = partial(GitLabNotificationApplication, preset=UNANNOUNCED_NOTIFICATIONS)


class GitLabNotificationUnannouncedAmbiguityProblem(GitLabNotificationAmbiguityProblem):
    application_class = partial(GitLabNotificationApplication, preset=UNANNOUNCED_AMBIGUOUS_NOTIFICATIONS)


# Existing class names remain available to callers.
GitLabNotificationUnannounced = GitLabNotificationUnannouncedProblem
GitLabNotificationUnannouncedAmbiguity = GitLabNotificationUnannouncedAmbiguityProblem
