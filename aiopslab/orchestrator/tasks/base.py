# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from aiopslab.config import Config
from aiopslab.paths import BASE_DIR
from aiopslab.service.kubectl import KubeCtl
from aiopslab.orchestrator.evaluators.quantitative import *
from aiopslab.orchestrator.evaluators.qualitative import LLMJudge


config = Config(BASE_DIR / "config.yml")


class Task:
    """Base class for all tasks."""

    def __init__(self):
        self.results = {}
        self.kubectl = KubeCtl()

    def get_task_description(self):
        raise NotImplementedError("Subclasses must implement this method.")

    def get_instructions(self):
        raise NotImplementedError("Subclasses must implement this method.")

    def get_available_actions(self):
        raise NotImplementedError("Subclasses must implement this method.")

    def perform_action(self, action_name, *args, **kwargs):
        raise NotImplementedError("Subclasses must implement this method.")

    def add_result(self, key, value):
        """Add an evaluation result to the task."""
        self.results[key] = value

    def common_eval(self, trace: list[SessionItem]):
        """Common evaluation function across tasks. Both quantitative and (optionally) qualitative evaluation.
        NOTE: This method must be called by the `eval` method of each task.

        Args:
            trace (list[SessionItem]): A list of session items.
                - each item corresponds to either the agent's or the env's response

        Returns:
            None
        """
        self.add_result("steps", num_steps_taken(trace))
        self.add_result("in_tokens", in_tokens(trace))
        self.add_result("out_tokens", out_tokens(trace))

        if config.get("qualitative_eval"):
            judge = LLMJudge(trace)
            score, judgement = judge.reasoning_score()
            self.add_result("reasoning_judgement", judgement)
            self.add_result("reasoning_score", score)

    def sys_status_after_recovery(self) -> bool:
        pod_list = self.kubectl.list_pods(self.namespace)
        all_normal = True

        for pod in pod_list.items:
            if pod.status.container_statuses:
                for container_status in pod.status.container_statuses:
                    if (
                        container_status.state.waiting
                        and container_status.state.waiting.reason == "CrashLoopBackOff"
                    ):
                        all_normal = False
                    elif (
                        container_status.state.terminated
                        and container_status.state.terminated.reason != "Completed"
                    ):
                        all_normal = False
                    elif not container_status.ready:
                        all_normal = False

            if not all_normal:
                break

        return all_normal
