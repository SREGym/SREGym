"""Does naming the mail subsystem explain the 3-of-3 on notification recovery?

Identical fault, identical grader, identical budget. The only change is that the
application description no longer says notifications exist, so delivering them
becomes something the responder has to notice rather than something it is told.

This is the cheapest available test of the finding that a pointer is
load-bearing: nine words naming stripe's workspace were worth 80 seconds and one
solve. Two rungs are built so the result is not a single data point -- if only
the lower one moves, the effect is about noticing; if both move by the same
amount, it is about the time the search costs.
"""

from sregym.conductor.problems.gitlab_notification_ambiguity import GitLabNotificationAmbiguity
from sregym.conductor.problems.gitlab_notification_recovery import GitLabNotificationRecovery
from sregym.service.apps.gitlab_notification_unannounced import (
    GitLabNotificationUnannounced as UnannouncedApplication,
)
from sregym.service.apps.gitlab_notification_unannounced import (
    GitLabNotificationUnannouncedAmbiguity as UnannouncedAmbiguityApplication,
)


class GitLabNotificationUnannounced(GitLabNotificationRecovery):
    application_class = UnannouncedApplication


class GitLabNotificationUnannouncedAmbiguity(GitLabNotificationAmbiguity):
    application_class = UnannouncedAmbiguityApplication
