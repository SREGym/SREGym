"""Strict mode for Slack Spine."""

from __future__ import annotations

from sregym.conductor.problems.apps.slack_spine.base import SlackSpineProblem
from sregym.conductor.problems.apps.slack_spine.events import _acl_event
from sregym.conductor.problems.apps.slack_spine.pools import RolePoolSize


class SlackSendsFailStrictMode(SlackSpineProblem):
    """Incident Arena 013: channel strict-ACL toggle + a 3/2 channel pool."""

    TASK = "013--slack-spine--06-sends-fail-after-strict-mode-turns-on-728f0739"
    PROBLEM_ID = "slack_sends_fail_strict_mode"

    def build_legs(self):
        return [_acl_event(), RolePoolSize("channel", 3, 2)]


class SlackSendsFailComplianceWindow(SlackSpineProblem):
    """Incident Arena 014: the strict-ACL window is mandated; only the 3/2 channel pool may change."""

    TASK = "014--slack-spine--06-sends-fail-during-compliance-window-1f4b1235"
    PROBLEM_ID = "slack_sends_fail_compliance_window"

    def build_legs(self):
        # The mandated window is context, not a cause: only the pool is graded.
        return [_acl_event(keep_active=True), RolePoolSize("channel", 3, 2)]
