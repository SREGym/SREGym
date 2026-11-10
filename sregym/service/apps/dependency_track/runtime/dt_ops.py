"""Grader-side Dependency-Track operations (bootstrap, probes, reference repair).

Piped to ``python -`` in the ci-pipelines pod on stdin together with a call
expression; never written to the cluster. Uses only the standard library.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

DT = "http://dt-api-server:8080"
TRACKER = "http://violation-tracker:8080"
CI_HOOK = "http://127.0.0.1:8080/hooks/git"
WEBHOOK_PUBLISHER = "webhook"
USER_AGENT = "acme-secops/1.0"
SECOPS_PERMISSIONS = [
    "VIEW_PORTFOLIO",
    "VIEW_POLICY_VIOLATION",
    "POLICY_VIOLATION_ANALYSIS",
    "POLICY_MANAGEMENT_READ",
    "PORTFOLIO_MANAGEMENT_READ",
    "SYSTEM_CONFIGURATION_READ",
]

POLICIES = [
    {
        "name": "License must be declared",
        "operator": "ANY",
        "violationState": "WARN",
        "conditions": [{"subject": "LICENSE", "operator": "IS", "value": "unresolved"}],
    },
    {
        "name": "Strong copyleft licenses",
        "operator": "ANY",
        "violationState": "FAIL",
        "conditions": [{"subject": "LICENSE_GROUP", "operator": "IS", "value": "@group:Copyleft"}],
    },
    {
        "name": "Deprecated packages",
        "operator": "ANY",
        "violationState": "WARN",
        "conditions": [
            {"subject": "PACKAGE_URL", "operator": "MATCHES", "value": "pkg:npm/moment@.*"},
            {"subject": "PACKAGE_URL", "operator": "MATCHES", "value": "pkg:npm/request@.*"},
            {"subject": "PACKAGE_URL", "operator": "MATCHES", "value": "pkg:maven/com.h2database/h2@.*"},
        ],
    },
]
RULE_NAME = "security-alerts"


class Http:
    def __init__(self, base, headers=None):
        self.base = base
        self.headers = {"User-Agent": USER_AGENT, **(headers or {})}

    def call(self, method, path, body=None, form=None, timeout=120, raw=False):
        headers = {**self.headers}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        deadline = time.time() + max(timeout, 180)
        while True:
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    text = response.read().decode()
                    if raw:
                        return response.status, response.headers, text
                    return response.status, response.headers, (json.loads(text) if text and text[0] in "[{" else text)
            except urllib.error.HTTPError as error:
                if error.code in (502, 503, 504) and time.time() < deadline:
                    time.sleep(3)
                    continue
                return error.code, error.headers, error.read().decode()
            except (urllib.error.URLError, OSError):
                # The API may be restarting or not yet behind its Service.
                if time.time() >= deadline:
                    raise
                time.sleep(3)

    def ok(self, method, path, body=None, form=None, timeout=120, expect=(200, 201, 202, 204)):
        status, headers, payload = self.call(method, path, body, form, timeout)
        if status not in expect:
            raise RuntimeError(f"{method} {path} -> {status}: {str(payload)[:500]}")
        return payload

    def paged(self, path, page_size=500):
        items, page = [], 1
        separator = "&" if "?" in path else "?"
        while True:
            status, headers, batch = self.call("GET", f"{path}{separator}pageSize={page_size}&pageNumber={page}")
            if status != 200:
                raise RuntimeError(f"GET {path} -> {status}: {str(batch)[:300]}")
            items.extend(batch or [])
            if not batch or len(items) >= int(headers.get("X-Total-Count", "0")):
                return items
            page += 1


def wait_api(timeout=900):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _, body = Http(DT).call("GET", "/api/version", timeout=10)
            if status == 200 and isinstance(body, dict):
                return body
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(3)
    raise RuntimeError("Dependency-Track API did not answer")


def admin(credential, timeout=600):
    """A client for an API key (``odt_...``, no login is recorded) or for the admin password."""
    if credential.startswith("odt_"):
        return Http(DT, {"X-Api-Key": credential})
    password = credential
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            status, _, token = Http(DT).call(
                "POST", "/api/v1/user/login", form={"username": "admin", "password": password}
            )
            if status == 200:
                return Http(DT, {"Authorization": f"Bearer {token}"})
            last = (status, str(token)[:200])
        except (urllib.error.URLError, OSError) as error:
            last = str(error)
        time.sleep(5)
    raise RuntimeError(f"admin login failed: {last}")


def bootstrap(password, signing_secret, tracker_url):
    version = wait_api()
    status, _, body = Http(DT).call(
        "POST",
        "/api/v1/user/forceChangePassword",
        form={"username": "admin", "password": "admin", "newPassword": password, "confirmPassword": password},
    )
    if status not in (200, 401):
        raise RuntimeError(f"password change -> {status}: {body}")
    dt = admin(password)
    for point, name in (("vuln-data-source", "nvd"), ("kev-data-source", "cisa"), ("kev-data-source", "enisa")):
        config = dt.ok("GET", f"/api/v2/extension-points/{point}/extensions/{name}/config")["config"]
        config["enabled"] = False
        dt.ok("PUT", f"/api/v2/extension-points/{point}/extensions/{name}/config", {"config": config})
    keys = {}
    teams = {team["name"]: team for team in dt.ok("GET", "/api/v1/team")}
    wanted = {
        "CI Pipelines": ["BOM_UPLOAD", "PROJECT_CREATION_UPLOAD", "VIEW_PORTFOLIO", "VIEW_POLICY_VIOLATION"],
        "Security Ticketing": ["VIEW_PORTFOLIO", "VIEW_POLICY_VIOLATION"],
        "Security Operations": SECOPS_PERMISSIONS,
    }
    for name, permissions in wanted.items():
        team = teams.get(name) or dt.ok("PUT", "/api/v1/team", {"name": name})
        for permission in permissions:
            dt.call("POST", f"/api/v1/permission/{permission}/team/{team['uuid']}")
        keys[name] = dt.ok("PUT", f"/api/v1/team/{team['uuid']}/key")["key"]
    status, _, body = dt.call(
        "POST",
        "/api/v2/secrets",
        {
            "name": "tracker-signing-secret",
            "value": signing_secret,
            "description": "HMAC key for the security ticket bridge",
        },
    )
    if status not in (201, 409):
        raise RuntimeError(f"secret -> {status}: {body}")
    existing = {policy["name"] for policy in dt.paged("/api/v1/policy")}
    for spec in POLICIES:
        if spec["name"] in existing:
            continue
        policy = dt.ok("PUT", "/api/v1/policy", {k: spec[k] for k in ("name", "operator", "violationState")})
        for condition in spec["conditions"]:
            value = condition["value"]
            if value.startswith("@group:"):
                groups = {g["name"]: g for g in dt.paged("/api/v1/licenseGroup")}
                value = groups[value.removeprefix("@group:")]["uuid"]
            dt.ok("PUT", f"/api/v1/policy/{policy['uuid']}/condition", {**condition, "value": value})
    publishers = {p["extensionName"]: p for p in dt.ok("GET", "/api/v1/notification/publisher")}
    rules = {r["name"]: r for r in dt.paged("/api/v1/notification/rule")}
    rule = rules.get(RULE_NAME) or dt.ok(
        "PUT",
        "/api/v1/notification/rule",
        {
            "name": RULE_NAME,
            "scope": "PORTFOLIO",
            "notificationLevel": "INFORMATIONAL",
            "publisher": {"uuid": publishers[WEBHOOK_PUBLISHER]["uuid"]},
        },
    )
    dt.ok(
        "POST",
        "/api/v1/notification/rule",
        {
            "name": RULE_NAME,
            "enabled": True,
            "notifyChildren": True,
            "logSuccessfulPublish": False,
            "scope": "PORTFOLIO",
            "level": "INFORMATIONAL",
            "notifyOn": ["POLICY_VIOLATION"],
            "publisherConfig": json.dumps({"destinationUrl": tracker_url, "signingSecret": "tracker-signing-secret"}),
            "uuid": rule["uuid"],
        },
    )
    return {
        "version": version.get("version"),
        "ci_key": keys["CI Pipelines"],
        "tracker_key": keys["Security Ticketing"],
        "secops_key": keys["Security Operations"],
    }


def projects(password):
    dt = admin(password)
    return [
        {"uuid": p["uuid"], "name": p["name"], "version": p.get("version"), "active": p.get("active", True)}
        for p in dt.paged("/api/v1/project")
    ]


def project_uuid(password, name, version):
    status, _, body = admin(password).call(
        "GET", "/api/v1/project/lookup?" + urllib.parse.urlencode({"name": name, "version": version})
    )
    return body["uuid"] if status == 200 else None


def violations(password, uuid):
    dt = admin(password)
    found = []
    for v in dt.paged(f"/api/v1/violation/project/{uuid}?suppressed=true"):
        analysis = v.get("analysis") or {}
        found.append(
            {
                "uuid": v["uuid"],
                "component": v["component"]["uuid"],
                "condition": v["policyCondition"]["uuid"],
                "policy": v["policyCondition"]["policy"]["name"],
                "timestamp": v.get("timestamp"),
                "analysis": analysis.get("analysisState"),
            }
        )
    return found


def tickets(project=None):
    path = "/tickets" + (f"?project={project}" if project else "")
    return Http(TRACKER).ok("GET", path, timeout=60)


def tracker_audit():
    return Http(TRACKER).ok("GET", "/audit", timeout=30)


def push_tag(repository, ref):
    return Http("").ok("POST", CI_HOOK, {"repository": repository, "ref": ref, "pusher": "release-bot"})


def pipelines():
    return Http("").ok("GET", "http://127.0.0.1:8080/pipelines")


def configuration(password):
    dt = admin(password)
    policies = []
    for policy in dt.paged("/api/v1/policy"):
        policies.append(
            {
                "name": policy["name"],
                "operator": policy["operator"],
                "state": policy["violationState"],
                "conditions": sorted(
                    (c["subject"], c["operator"], c["value"]) for c in policy.get("policyConditions", [])
                ),
                "projects": len(policy.get("projects") or []),
                "tags": len(policy.get("tags") or []),
            }
        )
    rules = []
    for rule in dt.paged("/api/v1/notification/rule"):
        rules.append(
            {
                "name": rule["name"],
                "enabled": rule["enabled"],
                "scope": rule["scope"],
                "level": rule.get("notificationLevel"),
                "notifyOn": sorted(rule.get("notifyOn") or []),
                "publisher": (rule.get("publisher") or {}).get("extensionName"),
                "destination": json.loads(rule.get("publisherConfig") or "{}").get("destinationUrl"),
                "projects": len(rule.get("projects") or []),
                "tags": len(rule.get("tags") or []),
                "filter": rule.get("filterExpression"),
            }
        )
    return {"policies": sorted(policies, key=lambda p: p["name"]), "rules": sorted(rules, key=lambda r: r["name"])}


def component_count(password, uuid):
    status, headers, _ = admin(password).call("GET", f"/api/v1/component/project/{uuid}?pageSize=1&pageNumber=1")
    if status != 200:
        raise RuntimeError(f"component count -> {status}")
    return int(headers.get("X-Total-Count", "0"))


def project_exists(password, uuid):
    status, _, body = admin(password).call("GET", f"/api/v1/project/{uuid}")
    return status == 200 and isinstance(body, dict) and body.get("uuid") == uuid


def analyze(password, uuid):
    return admin(password).ok("POST", f"/api/v1/finding/project/{uuid}/analyze")


def workflow_runs(password, label_project=None, name=None, limit=50):
    query = {"limit": limit}
    if name:
        query["workflow_name"] = name
    if label_project:
        query["label"] = f"project_uuid={label_project}"
    status, _, body = admin(password).call("GET", "/api/v2/internal/workflow-runs?" + urllib.parse.urlencode(query))
    return body if status == 200 else {"error": status, "body": str(body)[:300]}


# ---------------------------------------------------------------- advisories and the gate policy
ADVISORY_POLICY = {
    "name": "Vulnerabilities rated high or critical",
    "operator": "ANY",
    "violationState": "FAIL",
    "conditions": [
        {"subject": "SEVERITY", "operator": "IS", "value": "CRITICAL"},
        {"subject": "SEVERITY", "operator": "IS", "value": "HIGH"},
    ],
}
#: The security team's internal advisories (vulnerability source INTERNAL), matched by package URL and version range.
ADVISORIES = [
    {
        "vulnId": "INT-2026-0088",
        "title": "commons-text 2.10: string interpolation evaluates script lookups from untrusted input",
        "description": "StringSubstitutor's default lookups include script and url; interpolating untrusted input "
        "executes code. Fixed in 2.11.0.",
        "severity": "CRITICAL",
        "cvssV3Vector": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
        "affectedComponents": [
            {
                "identityType": "PURL",
                "identity": "pkg:maven/org.apache.commons/commons-text",
                "versionType": "RANGE",
                "versionStartIncluding": "2.10.0",
                "versionEndExcluding": "2.11.0",
            }
        ],
    },
    {
        "vulnId": "INT-2026-0093",
        "title": "netty-buffer 6.2: out-of-bounds read in CompositeByteBuf.getBytes",
        "description": "A crafted sequence of component removals lets getBytes read past the end of a component. "
        "Fixed in 6.3.0.",
        "severity": "HIGH",
        "cvssV3Vector": "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:N/A:H",
        "affectedComponents": [
            {
                "identityType": "PURL",
                "identity": "pkg:maven/io.netty/netty-buffer",
                "versionType": "RANGE",
                "versionStartIncluding": "6.2.0",
                "versionEndExcluding": "6.3.0",
            }
        ],
    },
    {
        "vulnId": "INT-2026-0107",
        "title": "micromatch 6.27: catastrophic backtracking in brace expansion",
        "description": "Patterns with nested braces from user input make braces/micromatch backtrack exponentially "
        "and block the event loop. Fixed in 6.28.0.",
        "severity": "HIGH",
        "cvssV3Vector": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H",
        "affectedComponents": [
            {
                "identityType": "PURL",
                "identity": "pkg:npm/micromatch",
                "versionType": "RANGE",
                "versionStartIncluding": "6.27.0",
                "versionEndExcluding": "6.28.0",
            }
        ],
    },
]


def configure_advisories(password):
    """The security team's internal advisories and the policy that fails the gate of affected builds."""
    dt = admin(password)
    for advisory in ADVISORIES:
        status, _, body = dt.call("PUT", "/api/v1/vulnerability", advisory)
        if status not in (200, 201, 409):
            raise RuntimeError(f"advisory {advisory['vulnId']} -> {status}: {str(body)[:300]}")
    existing = {policy["name"] for policy in dt.paged("/api/v1/policy")}
    if ADVISORY_POLICY["name"] not in existing:
        policy = dt.ok("PUT", "/api/v1/policy", {k: ADVISORY_POLICY[k] for k in ("name", "operator", "violationState")})
        for condition in ADVISORY_POLICY["conditions"]:
            dt.ok("PUT", f"/api/v1/policy/{policy['uuid']}/condition", condition)
    return {"advisories": [a["vulnId"] for a in ADVISORIES]}


def findings(credential, uuid):
    dt = admin(credential)
    return [
        {
            "component": f["component"].get("purl"),
            "vulnerability": f["vulnerability"]["vulnId"],
            "severity": f["vulnerability"].get("severity"),
        }
        for f in dt.ok("GET", f"/api/v1/finding/project/{uuid}?suppressed=true") or []
    ]


# ---------------------------------------------------------------- profile: intake
#: The release's advisory, in addition to ADVISORIES.
INTAKE_ADVISORIES = [
    {
        "vulnId": "INT-2026-0114",
        "title": "moment 7.9: inefficient regular expression in RFC 2822 date parsing",
        "description": "moment(String) with untrusted input of a few kilobytes blocks the event loop for seconds "
        "(quadratic backtracking in the RFC 2822 pattern). Fixed in 7.10.0.",
        "severity": "HIGH",
        "cvssV3Vector": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H",
        "affectedComponents": [
            {
                "identityType": "PURL",
                "identity": "pkg:npm/moment",
                "versionType": "RANGE",
                "versionStartIncluding": "7.9.0",
                "versionEndExcluding": "7.10.0",
            }
        ],
    },
]
INTAKE_GROUPS = ["NEW_VULNERABLE_DEPENDENCY", "POLICY_VIOLATION"]


def configure_intake(password, tracker_url):
    """Advisories, the gate policy, and new-vulnerable-dependency notifications for the intake register."""
    configure_advisories(password)
    dt = admin(password)
    for advisory in INTAKE_ADVISORIES:
        status, _, body = dt.call("PUT", "/api/v1/vulnerability", advisory)
        if status not in (200, 201, 409):
            raise RuntimeError(f"advisory {advisory['vulnId']} -> {status}: {str(body)[:300]}")
    # The intake audit compares the portfolio's findings with the register.
    team = {t["name"]: t for t in dt.ok("GET", "/api/v1/team")}["Security Ticketing"]
    dt.call("POST", f"/api/v1/permission/VIEW_VULNERABILITY/team/{team['uuid']}")
    rule = {r["name"]: r for r in dt.paged("/api/v1/notification/rule")}[RULE_NAME]
    dt.ok(
        "POST",
        "/api/v1/notification/rule",
        {
            "name": RULE_NAME,
            "enabled": True,
            "notifyChildren": True,
            "logSuccessfulPublish": False,
            "scope": "PORTFOLIO",
            "level": "INFORMATIONAL",
            "notifyOn": INTAKE_GROUPS,
            "publisherConfig": json.dumps({"destinationUrl": tracker_url, "signingSecret": "tracker-signing-secret"}),
            "uuid": rule["uuid"],
        },
    )
    return {"advisories": [a["vulnId"] for a in ADVISORIES + INTAKE_ADVISORIES], "notifyOn": INTAKE_GROUPS}


def intake(project=None):
    path = "/intake" + (f"?project={project}" if project else "")
    return Http(TRACKER).ok("GET", path, timeout=60)


def push_main(repository, commits):
    """A push to a repository's main branch (``commits``: [{"id", "message"}])."""
    return Http("").ok(
        "POST", CI_HOOK, {"repository": repository, "ref": "refs/heads/main", "commits": commits, "pusher": "renovate"}
    )


def project_components(credential, uuid):
    dt = admin(credential)
    return [
        {"uuid": c["uuid"], "purl": c.get("purl"), "name": c.get("name"), "version": c.get("version")}
        for c in dt.paged(f"/api/v1/component/project/{uuid}")
    ]


def delete_component(password, uuid):
    return admin(password).ok("DELETE", f"/api/v1/component/{uuid}")


def export_bom(password, uuid):
    """The project's current CycloneDX BOM (JSON text)."""
    status, _, body = admin(password).call("GET", f"/api/v1/bom/cyclonedx/project/{uuid}?format=json", raw=True)
    if status != 200:
        raise RuntimeError(f"bom export -> {status}: {str(body)[:300]}")
    return body


def upload_bom(password, name, version, bom_json):
    import base64

    payload = {
        "projectName": name,
        "projectVersion": version,
        "bom": base64.b64encode(bom_json.encode()).decode(),
    }
    return admin(password).ok("PUT", "/api/v1/bom", payload)


def token_status(password, token):
    return admin(password).ok("GET", f"/api/v1/event/token/{token}")
