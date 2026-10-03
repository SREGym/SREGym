"""Candidate problems: the 3-of-3 families with one naming clause withheld.

Identical faults, graders and budgets. Each differs from its registered
counterpart by a single sentence of the application description, so whatever the
screens show is attributable to that sentence and nothing else.
"""

from sregym.conductor.problems.gitea_database_deletion import GiteaDatabaseDeletion
from sregym.conductor.problems.gitlab_database_deletion import GitLabDatabaseDeletion
from sregym.conductor.problems.mattermost_capacity_cascade import MattermostCapacityCascade
from sregym.service.apps.unannounced_families import (
    GiteaRecoveryUnannounced,
    GitLabRecoveryUnannounced,
    MattermostCascadeUnannounced,
)


class GiteaDatabaseDeletionUnannounced(GiteaDatabaseDeletion):
    application_class = GiteaRecoveryUnannounced


class GitLabDatabaseDeletionUnannounced(GitLabDatabaseDeletion):
    application_class = GitLabRecoveryUnannounced


class MattermostCapacityCascadeUnannounced(MattermostCapacityCascade):
    application_class = MattermostCascadeUnannounced
