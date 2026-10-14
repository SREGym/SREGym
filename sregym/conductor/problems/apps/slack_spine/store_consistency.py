"""Store consistency for Slack Spine."""

from __future__ import annotations

from sregym.conductor.problems.apps.slack_spine.base import SlackSpineProblem
from sregym.conductor.problems.apps.slack_spine.events import _store_event


class SlackLoginsUnreadSendsAllSlow(SlackSpineProblem):
    """Incident Arena 009: shared Redis store put into strict mode (250ms hold) on three services."""

    TASK = "009--slack-spine--06-logins-unread-and-sends-all-slow-since-noon-16e3fd23"
    PROBLEM_ID = "slack_logins_unread_sends_all_slow"

    def build_legs(self):
        return [_store_event(["auth", "workspace", "notification"], "250")]
