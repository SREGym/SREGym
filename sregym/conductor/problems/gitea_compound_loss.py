"""One incident, two faults: one that announces itself and one that does not.

The shape that separated the agent on `stripe_feature_config` was silent
incompleteness -- repairing the visible fault restored every signal the agent
thought to check, while a second obligation remained. That one came about by
accident. This builds it deliberately, and by mechanism rather than by
withholding a sentence, so it holds even if the description-pointer lever turns
out to be weak.

The maintenance reset deletes the database schema, which is loud: Gitea returns
500s, `\\dt` is empty, and the recovery path is obvious. The same reset also
discards one repository's git storage, which is silent: once the database is
restored Gitea serves, the issue journal reconciles, every repository is listed,
and only that one repository's files are gone. `acknowledged_git_data_changed`
is already graded, so a recovery that stops at the database is a named failure
rather than a surprise.

It stays solvable: a local snapshot of the repository storage is on the data
volume, and the recovery path is the same `tar` the backup was made with.
"""

import json

from sregym.conductor.problems.gitea_database_deletion import GiteaDatabaseDeletion
from sregym.service.apps.gitea import FIXTURES
from sregym.utils.decorators import mark_fault_injected


class GiteaCompoundLoss(GiteaDatabaseDeletion):
    def __init__(self, scale_tier="replicated"):
        super().__init__(scale_tier)
        self.discarded_repository = None
        self.root_cause = self.build_structured_root_cause(
            component="gitea-db",
            namespace=self.namespace,
            description=(
                "A maintenance reset ran against Gitea's writable primary, deleting the public schema, and the "
                "same operation discarded one repository's git storage. Restoring the database returns the "
                "service to health and leaves the repository empty. Recover both the acknowledged database "
                "state and the repository contents from the storage snapshot."
            ),
        )
        self.diagnosis_oracle.expected = self.root_cause

    def repositories(self):
        fixture = json.loads((FIXTURES / "import-data.json").read_text())
        return [r["owner"] + "/" + r["name"] for r in fixture["repositories"]]

    def prepare_recovery_material(self):
        super().prepare_recovery_material()
        # Taken after the acknowledged writes, so the snapshot is a complete
        # record of the repository storage at the moment of the incident.
        self.app.backup_git_repositories()

    @mark_fault_injected
    def inject_fault(self):
        super().inject_fault()
        # The second, quiet half of the same operation. Chosen deterministically
        # so a failure names the same repository every time.
        self.discarded_repository = self.repositories()[0]
        self.app.discard_repository_storage(self.discarded_repository)
        inventory = self.app.git_inventory()
        if inventory.get(self.discarded_repository):
            raise RuntimeError(f"Repository storage for {self.discarded_repository} survived the incident")
        if not self.expected_git.get(self.discarded_repository):
            raise RuntimeError("The pre-incident inventory recorded no files for the discarded repository")

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        """Database first, then storage.

        Extracting the snapshot needs a running Gitea pod, and a deleted schema
        can leave it crashlooping, so repairing the database has to come first --
        `restore_archive` brings the deployment back up on its way out. Reversing
        these two turns a recoverable state into a failed teardown, which voids
        the attempt instead of grading it.
        """
        # The parent is decorated too; nesting is safe because the decorator only
        # records state and re-raises, it asserts nothing about the prior value.
        GiteaDatabaseDeletion.recover_fault(self)
        if self.discarded_repository:
            self.app.resume_application()
            if not self.mitigation_oracle.evaluate().get("success"):
                self.app.restore_git_repositories()
        self.discarded_repository = None
