"""Factory that picks the right issue parser for a given URL.

Usage:
    from sregym.service.issue_parser import parse_issue

    parsed = parse_issue("https://github.com/apache/cassandra/issues/20108")
    parsed = parse_issue("https://issues.apache.org/jira/browse/CASSANDRA-20108")
"""

from sregym.service.github_issue_parser import GitHubIssueParser, ParsedIssue
from sregym.service.jira_issue_parser import JiraIssueParser

__all__ = ["parse_issue", "ParsedIssue"]


def parse_issue(url: str) -> ParsedIssue:
    """Parse a GitHub or Jira issue URL and return a ParsedIssue."""
    url = url.strip()

    if "github.com" in url:
        return GitHubIssueParser(url).resolve()

    if "/browse/" in url:
        return JiraIssueParser(url).resolve()

    raise ValueError(
        f"Unrecognised issue URL: {url!r}\n"
        f"Supported formats:\n"
        f"  GitHub: https://github.com/owner/repo/issues/123\n"
        f"  Jira:   https://issues.apache.org/jira/browse/PROJECT-123\n"
        f"          https://company.atlassian.net/browse/PROJECT-123"
    )
