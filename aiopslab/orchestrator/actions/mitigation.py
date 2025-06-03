# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Actions for the mitigation task."""

from aiopslab.orchestrator.actions.base import TaskActions
from aiopslab.utils.actions import action
from aiopslab.utils.status import SubmissionStatus


class MitigationActions(TaskActions):
    """
    Class for mitigation task's actions.
    """

    @staticmethod
    @action
    def submit() -> SubmissionStatus:
        """
        Submit once your mitigation solution is complete and ready to be evaluated.

        Args:
            None

        Returns:
            SubmissionStatus: The status of the submission.
        """
        # for mitigation task, the submission is valid if the solution is submitted
        # NOTE: this does not mean the solution is correct!
        return SubmissionStatus.VALID_SUBMISSION
