"""The same mail subsystem, not named in the application description.

`GitLabNotificationRecovery` describes its own mail path to the responder:

    Issue notifications are delivered over SMTP through a Sidekiq mailers queue
    to a local provider with a read-only delivery audit.

That sentence is true, and the families carrying it were solved 3 of 3. It also
tells the responder that notifications are part of the recovery, which is the
only hard part of the task -- a deleted database is loud and self-announcing,
while an undelivered mail queue is not. Removing it is the difference between
"reconcile the mail" and "notice that there is mail".

Nothing is hidden. The `notification-mailbox` deployment, its service and its
volume are all visible to `kubectl`, Sidekiq's queues are readable, and the
delivery audit answers on the port it always did. Only the narration is gone.
"""

from sregym.service.apps.gitlab_notification_ambiguity import GitLabNotificationAmbiguity
from sregym.service.apps.gitlab_notification_recovery import GitLabNotificationRecovery
from sregym.service.apps.unannounced import Unannounced

#: The clause each announced application appends to its description.
ANNOUNCEMENT = (
    " Issue notifications are delivered over SMTP through a Sidekiq mailers queue"
    " to a local provider with a read-only delivery audit."
)


class GitLabNotificationUnannounced(Unannounced, GitLabNotificationRecovery):
    REMOVED_CLAUSE = ANNOUNCEMENT


class GitLabNotificationUnannouncedAmbiguity(Unannounced, GitLabNotificationAmbiguity):
    REMOVED_CLAUSE = ANNOUNCEMENT
