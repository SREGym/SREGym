# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Actions for the root-cause analysis task."""

from aiopslab.orchestrator.actions.base import TaskActions
from aiopslab.utils.actions import action
from aiopslab.utils.status import SubmissionStatus


class AnalysisActions(TaskActions):
    """
    Class for root-cause analysis task's actions.
    """

    @staticmethod
    @action
    def submit(analysis: dict[str, str]) -> SubmissionStatus:
        """Submit the analysis solution to the orchestrator for evaluation.

        Args:
            analysis (dict[str]): A dictionary with two keys: 'system_level' and 'fault_type'.

        Returns:
            SubmissionStatus: The status of the submission.
        """
        # TODO: check if analysis is in the correct format
        return SubmissionStatus.VALID_SUBMISSION
