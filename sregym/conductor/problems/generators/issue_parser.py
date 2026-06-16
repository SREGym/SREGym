"""Fetch a JIRA or GitHub issue and extract a structured problem spec using Claude.

Usage:
    from sregym.conductor.problems.generators.issue_parser import parse_issue
    spec = parse_issue("https://issues.apache.org/jira/browse/CASSANDRA-20108")
"""

from __future__ import annotations

import json
import os
import re
from urllib.parse import urlparse

import anthropic
import requests

_EXTRACTION_PROMPT = """\
You are an expert in distributed systems bugs. Given the text of a bug report (JIRA or GitHub issue),
extract a structured problem spec as JSON.

Rules:
- `version`: the LAST affected version (e.g. the highest buggy release, like "4.1.7")
- `git_ref`: the git tag for that version (e.g. "cassandra-4.1.7" for Cassandra, "v7.1.1" for TiDB)
- `root_cause_file`: the source file containing the bug — infer from stack traces in the issue
  (e.g. "src/java/org/apache/cassandra/db/filter/RowFilter.java"). Use the deepest frame before
  the exception, not a framework/JVM frame. If not determinable, use "unknown".
- `root_cause_description`: 2-3 sentences describing the root cause, the trigger, and the fix.
  Be specific: name the class, method, and the invariant that is violated.
- `expected_exception`: the exception class that appears in logs when the bug fires (e.g.
  "TombstoneOverwhelmingException", "IndexOutOfBoundsException", "AssertionError").
  If the bug causes a crash without a named exception, use "AssertionError" or "Error".
- `trigger_cql`: complete CQL (for Cassandra) or SQL (for TiDB) statements that reproduce the bug,
  as a single string with statements separated by semicolons. Include CREATE KEYSPACE/TABLE,
  INSERT/UPDATE, and the query that triggers the bug. If the issue has an exact repro, use it
  verbatim. Must be executable as-is against a fresh cluster.
- `background_select`: the single SELECT statement (fully qualified with keyspace/db) that
  continuously triggers the bug in a background loop. If the bug is a crash (not a query error),
  this can be null.
- `needs_background_loop`: true if the fault should be continuously re-triggered every 15s for
  observability (query errors, metrics); false if it is a one-shot crash (CrashLoopBackOff).
- `python_class_name`: PascalCase class name, e.g. "Cassandra20108"
- `registry_key`: snake_case registry key, e.g. "cassandra_20108_filter_deleted_columns"
- `module_filename`: snake_case Python filename without .py, e.g. "cassandra_20108"
- `docstring`: one paragraph suitable for a Python docstring describing the bug, affected versions,
  observable symptoms, and root cause file.
- `system`: "cassandra" or "tidb"

Respond ONLY with valid JSON — no markdown, no commentary.

JSON schema:
{
  "system": string,
  "jira_id": string,
  "python_class_name": string,
  "registry_key": string,
  "module_filename": string,
  "version": string,
  "git_ref": string,
  "root_cause_file": string,
  "root_cause_description": string,
  "expected_exception": string,
  "trigger_cql": string,
  "background_select": string | null,
  "needs_background_loop": boolean,
  "docstring": string
}

Issue text:
{issue_text}
"""


def parse_issue(url: str, system: str | None = None) -> dict:
    """Fetch a JIRA or GitHub issue and return a problem spec dict.

    Args:
        url: GitHub issue URL or Apache JIRA issue URL.
        system: Override the system name ("cassandra", "tidb"). If None, inferred from the URL.

    Returns:
        dict matching the spec schema above.
    """
    issue_text = _fetch_issue_text(url)
    if system is None:
        system = _infer_system(url)
    return _extract_spec(issue_text, system, url)


# ---------------------------------------------------------------------------
# Issue fetchers
# ---------------------------------------------------------------------------


def _fetch_issue_text(url: str) -> str:
    parsed = urlparse(url)
    if "github.com" in parsed.netloc:
        return _fetch_github_issue(url)
    elif "jira" in parsed.netloc or "/jira/" in parsed.path or "issues.apache.org" in parsed.netloc:
        return _fetch_jira_issue(url)
    else:
        raise ValueError(f"Unsupported issue URL (expected GitHub or Apache JIRA): {url}")


def _fetch_github_issue(url: str) -> str:
    # URL format: https://github.com/owner/repo/issues/N
    match = re.match(r"https://github\.com/([^/]+)/([^/]+)/issues/(\d+)", url)
    if not match:
        raise ValueError(f"Cannot parse GitHub issue URL: {url}")
    owner, repo, number = match.groups()

    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    issue_resp = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/issues/{number}",
        headers=headers,
        timeout=30,
    )
    issue_resp.raise_for_status()
    issue = issue_resp.json()

    comments_resp = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments",
        headers=headers,
        timeout=30,
    )
    comments_resp.raise_for_status()

    parts = [
        f"GitHub Issue: {owner}/{repo}#{number}",
        f"Title: {issue['title']}",
        f"State: {issue['state']}",
        f"\n## Body\n{issue.get('body', '')}",
    ]
    for c in comments_resp.json()[:20]:
        parts.append(f"\n### Comment by {c['user']['login']}\n{c['body']}")
    return "\n".join(parts)


def _fetch_jira_issue(url: str) -> str:
    # URL format: https://issues.apache.org/jira/browse/CASSANDRA-16086
    match = re.search(r"/browse/([A-Z]+-\d+)", url)
    if not match:
        raise ValueError(f"Cannot extract JIRA key from URL: {url}")
    key = match.group(1)

    base = url.split("/browse/")[0]
    api_url = f"{base}/rest/api/2/issue/{key}?expand=renderedFields,comments"

    resp = requests.get(api_url, headers={"Accept": "application/json"}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    fields = data["fields"]

    fix_versions = [v["name"] for v in fields.get("fixVersions", [])]
    affected_versions = [v["name"] for v in fields.get("versions", [])]

    parts = [
        f"JIRA Issue: {key}",
        f"Summary: {fields['summary']}",
        f"Status: {fields['status']['name']}",
    ]
    if affected_versions:
        parts.append(f"Affected Versions: {', '.join(affected_versions)}")
    if fix_versions:
        parts.append(f"Fix Versions: {', '.join(fix_versions)}")

    description = fields.get("description") or ""
    parts.append(f"\n## Description\n{description}")

    comments = fields.get("comment", {}).get("comments", [])
    for c in comments[:25]:
        author = c.get("author", {}).get("displayName", "Unknown")
        parts.append(f"\n### Comment by {author}\n{c.get('body', '')}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Claude extraction
# ---------------------------------------------------------------------------


def _extract_spec(issue_text: str, system: str, source_url: str) -> dict:
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    prompt = _EXTRACTION_PROMPT.replace("{issue_text}", issue_text)

    message = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    # Strip markdown fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    spec = json.loads(raw)
    spec["source_url"] = source_url
    if "system" not in spec or not spec["system"]:
        spec["system"] = system
    return spec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _infer_system(url: str) -> str:
    url_lower = url.lower()
    if "cassandra" in url_lower:
        return "cassandra"
    if "tidb" in url_lower or "pingcap" in url_lower:
        return "tidb"
    return "cassandra"
