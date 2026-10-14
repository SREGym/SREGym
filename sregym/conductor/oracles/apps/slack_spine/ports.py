"""Slack Spine: ports."""

from __future__ import annotations

from sregym.conductor.oracles.incorrect_port import IncorrectPortAssignmentMitigationOracle
from sregym.conductor.problems.apps.slack_spine.manifests import _URL_PORT


class _UrlPortOracle(IncorrectPortAssignmentMitigationOracle):
    """The dependency address is a URL (``redis://redis:6379``): probe its host:port."""

    def _configured_address(self, deployment) -> str | None:
        value = super()._configured_address(deployment)
        match = _URL_PORT.match(value or "")
        return f"{match['host']}:{match['port']}" if match else value
