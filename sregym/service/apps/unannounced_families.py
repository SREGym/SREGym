"""The remaining 3-of-3 families, with their one naming clause withheld.

Each pairs with a registered family that differs only in this sentence, so the
pair is a controlled comparison of what naming a subsystem is worth.
"""

from sregym.service.apps.gitea_recovery import GiteaRecovery
from sregym.service.apps.gitlab_recovery import GitLabRecovery
from sregym.service.apps.mattermost_cascade import MattermostCascade
from sregym.service.apps.unannounced import Unannounced

#: Both recovery families point at the volume holding the backups and journals.
ARCHIVE_CLAUSE = " The recovery-console pod mounts the database archive volume at /recovery."

#: The cascade names the autoscaler, which is the component the incident is about.
CASCADE_CLAUSE = (
    " Customer traffic reaches chat through a `chat-gateway` fronting service"
    " with a bounded worker pool, and a `capacity-scaler` that adjusts its"
    " replica count automatically."
)


class GiteaRecoveryUnannounced(Unannounced, GiteaRecovery):
    """The archives are still mounted; the description no longer says where."""

    REMOVED_CLAUSE = ARCHIVE_CLAUSE


class GitLabRecoveryUnannounced(Unannounced, GitLabRecovery):
    REMOVED_CLAUSE = ARCHIVE_CLAUSE


class MattermostCascadeUnannounced(Unannounced, MattermostCascade):
    """The scaler still runs and is still the cause; it is no longer named.

    This is the sharpest of the three. The cascade is solved in four minutes by
    an agent told that a `capacity-scaler` adjusts replica counts automatically,
    because that sentence turns "find out why capacity is shrinking" into "go
    look at the scaler".
    """

    REMOVED_CLAUSE = CASCADE_CLAUSE
