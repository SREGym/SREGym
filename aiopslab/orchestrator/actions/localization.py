# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Actions for the localization task."""

from aiopslab.orchestrator.actions.base import TaskActions
from aiopslab.utils.actions import action
from aiopslab.utils.status import SubmissionStatus


class LocalizationActions(TaskActions):
    """
    Class for localization task's actions.
    """

    @staticmethod
    @action
    def submit(faulty_components: list[str]) -> SubmissionStatus:
        """
        Submit the detected faulty components to the orchestrator for evaluation.

        Args:
            faulty_components (list[str]): List of faulty components (i.e., service names).

        Returns:
            SubmissionStatus: The status of the submission.
        """
        return SubmissionStatus.VALID_SUBMISSION
