"""Resolve a Jira issue URL to the buggy database version and git ref.

Supports:
  - Apache Jira:       https://issues.apache.org/jira/browse/CASSANDRA-20108
  - Atlassian Cloud:   https://company.atlassian.net/browse/PROJ-123
  - Self-hosted Jira:  https://jira.company.com/browse/PROJ-123

Auth (all optional — Apache Jira is public):
  JIRA_TOKEN      Personal access token  →  Authorization: Bearer <token>
  JIRA_EMAIL      Atlassian Cloud email  ┐  Authorization: Basic base64(email:token)
  JIRA_API_TOKEN  Atlassian Cloud token  ┘

Version resolution fallback chain:
  1. Structured "Affects Version/s" field (most reliable — Jira-native)
  2. Derived from structured "Fix Version/s" field
  3. Semver found in summary or description text
  4. LLM fallback
"""

import json
import logging
import os
import re
import urllib.error
import urllib.request

from sregym.service.db_build_spec import DB_REGISTRY, DBBuildSpec
from sregym.service.github_issue_parser import ParsedIssue
from sregym.service.reproducer_extractor import extract_reproducer_full

logger = logging.getLogger(__name__)

_VERSION_RE = re.compile(r"\b(\d+\.\d+(?:\.\d+)*)\b")
_CONCRETE_VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)*$")
_RELEASED_PATCH_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# Matches Jira issue keys: PROJECT-123
_ISSUE_KEY_RE = re.compile(r"([A-Z][A-Z0-9]+)-(\d+)")


class JiraIssueParser:
    def __init__(self, issue_url: str):
        self.issue_url = issue_url
        self.base_url, self.issue_key = self._parse_url(issue_url)
        self.project_key = self.issue_key.split("-")[0].upper()
        self._token = os.environ.get("JIRA_TOKEN", "")
        self._email = os.environ.get("JIRA_EMAIL", "")
        self._api_token = os.environ.get("JIRA_API_TOKEN", "")

    def resolve(self) -> ParsedIssue:
        spec = self._lookup_spec()
        issue = self._fetch(f"/rest/api/2/issue/{self.issue_key}")
        fields = issue.get("fields", {})

        title = fields.get("summary", "")
        body = fields.get("description", "") or ""

        version = self._extract_version(fields, title, body, spec)
        git_ref = spec.git_ref(version)
        (
            reproducer,
            expected_output,
            buggy_output,
            correct_output,
            setup_preconditions,
            crash_on_startup,
            fault_injection_type,
        ) = extract_reproducer_full(body)
        if reproducer or crash_on_startup or fault_injection_type:
            logger.info(
                f"[JiraParser] Extracted reproducer ({len(reproducer) if reproducer else 0} chars)"
                + (f", expected_output={expected_output!r}" if expected_output else "")
                + (f", buggy_output={buggy_output!r}" if buggy_output else "")
                + (f", correct_output={correct_output!r}" if correct_output else "")
                + (f", setup_preconditions={setup_preconditions!r}" if setup_preconditions else "")
                + (", crash_on_startup=True" if crash_on_startup else "")
                + (f", fault_injection_type={fault_injection_type!r}" if fault_injection_type else "")
            )

        logger.info(f"[JiraParser] {self.issue_url} → db={spec.name} version={version} ref={git_ref}")
        return ParsedIssue(
            spec=spec,
            version=version,
            git_ref=git_ref,
            is_commit=False,
            title=title,
            body=body,
            reproducer=reproducer,
            expected_output=expected_output,
            crash_on_startup=crash_on_startup,
            buggy_output=buggy_output,
            correct_output=correct_output,
            setup_preconditions=setup_preconditions,
            fault_injection_type=fault_injection_type,
        )

    # ── Version extraction ────────────────────────────────────────────────────

    def _extract_version(self, fields: dict, title: str, body: str, spec: DBBuildSpec | None = None) -> str:
        # 1. Structured "Affects Version/s" — this is the buggy version directly.
        for v in fields.get("versions", []):
            name = str(v.get("name", "")).strip()
            if _CONCRETE_VERSION_RE.fullmatch(name):
                logger.debug(f"[JiraParser] version from affects-versions field: {name}")
                return name

        # 2. Structured "Fix Version/s" — derive the deployable buggy version by
        # taking the patch immediately before the fix on the oldest affected release line.
        released_patch_versions = []
        for v in fields.get("fixVersions", []):
            name = str(v.get("name", "")).strip()
            if _RELEASED_PATCH_VERSION_RE.fullmatch(name):
                released_patch_versions.append((tuple(int(part) for part in name.split(".")), name))
        if released_patch_versions:
            (major, minor, patch), fixed_version = min(released_patch_versions)
            if patch > 0:
                derived = f"{major}.{minor}.{patch - 1}"
                logger.info(
                    "[JiraParser] derived buggy version %s from fix version %s on oldest release line",
                    derived,
                    fixed_version,
                )
                return derived

        # 3. Semver in title or description
        for text in (title, body):
            version = self._find_version_in_text(text)
            if version:
                logger.debug(f"[JiraParser] version from text: {version}")
                return version

        # 4. LLM fallback
        text = f"{title}\n{body}"
        version = self._llm_extract_version(text, spec)
        if version:
            return version

        raise ValueError(
            f"Could not extract version from Jira issue {self.issue_key}. "
            f"Ensure the issue has an 'Affects Version/s' or 'Fix Version/s' field set."
        )

    def _find_version_in_text(self, text: str) -> str | None:
        # 1. Prefer explicit "v{major}.{minor}.{patch}" tags (e.g. "v7.5.1")
        m = re.search(r"\bv(\d+\.\d+\.\d+)\b", text)
        if m:
            return m.group(1)
        # 2. Lines that look like "affects version X" or "version: X"
        for line in text.splitlines():
            if re.search(r"affect|version|fixed|reported", line, re.IGNORECASE):
                m = _VERSION_RE.search(line)
                if m:
                    return m.group(1)
        # 3. First semver-ish anywhere in the text
        m = _VERSION_RE.search(text)
        return m.group(1) if m else None

    def _llm_extract_version(self, text: str, spec: DBBuildSpec | None) -> str | None:
        """Use LLM to extract the buggy version from the issue text."""
        if spec is None:
            return None
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return None
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key)
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=32,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"What version of {spec.name} contains the bug described in this issue?\n"
                            f"Reply with ONLY the version number (e.g. '7.5.1'), no 'v' prefix, no other text.\n\n"
                            f"---\n{text[:4000]}\n---"
                        ),
                    }
                ],
            )
            raw = message.content[0].text.strip()
            logger.info(f"[JiraParser] LLM raw version response: {raw!r}")
            m = re.search(r"(\d+\.\d+(?:\.\d+)*)", raw)
            if m:
                version = m.group(1)
                logger.info(f"[JiraParser] LLM extracted version: {version}")
                return version
        except Exception as e:
            logger.warning(f"[JiraParser] LLM version extraction failed: {e}")
        return None

    # ── Spec lookup ───────────────────────────────────────────────────────────

    def _lookup_spec(self) -> DBBuildSpec:
        for spec in DB_REGISTRY.values():
            if spec.jira_project and spec.jira_project.upper() == self.project_key:
                return spec
        raise ValueError(
            f"No DBBuildSpec registered with jira_project='{self.project_key}'. "
            f"Add jira_project='{self.project_key}' to an entry in DB_REGISTRY."
        )

    # ── Jira REST API ─────────────────────────────────────────────────────────

    def _fetch(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        headers = {"Accept": "application/json"}

        if self._email and self._api_token:
            # Atlassian Cloud: Basic auth with email + API token
            import base64

            creds = base64.b64encode(f"{self._email}:{self._api_token}".encode()).decode()
            headers["Authorization"] = f"Basic {creds}"
        elif self._token:
            # Server / Data Center: personal access token
            headers["Authorization"] = f"Bearer {self._token}"

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise RuntimeError(f"Jira API {url} → {e.code}: {body}") from e

    # ── URL parsing ───────────────────────────────────────────────────────────

    @staticmethod
    def _parse_url(url: str) -> tuple[str, str]:
        """Return (base_url, issue_key) from any Jira browse URL."""
        # Handles: .../jira/browse/KEY-123  and  .../browse/KEY-123
        m = re.search(
            r"(https?://[^/]+(?:/jira)?)/browse/([A-Z][A-Z0-9]+-\d+)",
            url,
            re.IGNORECASE,
        )
        if not m:
            raise ValueError(
                f"Not a recognised Jira issue URL: {url}\n"
                f"Expected format: https://issues.apache.org/jira/browse/PROJECT-123"
            )
        return m.group(1), m.group(2).upper()
