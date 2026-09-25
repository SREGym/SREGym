"""Derived signals computed in code over the compact component records.

Everything here is deterministic post-processing of what the collector already
gathered. It runs at the end of a live collection and, identically, over a saved
snapshot (see replay.py), so question design can be evaluated offline.

The derivations encode general SRE triage knowledge, not knowledge of any
particular fault:

* Error logs are classified into semantic classes (authentication, RBAC denial,
  DNS, data corruption, connectivity, overload) and the component each error
  names is resolved, so a component that other components' errors point at
  receives a structural signal. Only errors that are still occurring count;
  errors that stopped are history.
* A server that rejects logins is reporting on its clients' credentials, so the
  rejection points at the clients that present them, not at the server.
* A Service whose selector matches several workloads, a ResourceQuota that
  rejects pods for missing fields, and spec settings that break in-cluster
  networking become signals instead of buried detail.
* Warning events about pods that no longer exist are history, not evidence.
* Observability components are tagged so the model can apply the right prior.
* Recorded changes are ranked against the first active symptom (changes.py).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

from clients.jev_diag import changes as change_log
from clients.jev_diag.timeutil import now, parse_time

OBSERVABILITY_RE = re.compile(
    r"(grafana|jaeger|prometheus|otel|opentelemetry|opensearch|elasticsearch|kibana|loki|fluent|promtail|"
    r"alertmanager|kube-state-metrics|node-exporter|tempo|zipkin|pushgateway|blackbox)",
    re.I,
)

# A failed attempt to connect, in the words applications commonly use: a failure word shortly before a form of
# "connect", or a form of "connect" shortly before a failure word.
_FAILURE_WORDS = r"(?:\b(?:fail(?:ed|ure|s|ing)?|unable|cannot|can'?t|could\s?n[o']?t|not\s+able|error)\b|n't\s+able\b)"
_CONNECT_FAILURE = (
    rf"{_FAILURE_WORDS}(?:\W+\w+){{0,4}}?\W+connect\w*|"
    r"\bconnect(?:ion|ing|ed)?\b(?:\W+\w+){0,4}?\W+(?:fail\w*|refused|error|timed?\s?out|reset|lost|closed|aborted)\b"
)

# Semantic classes of error lines. Dependency classes describe a failed call to another component and
# therefore point at that component; own-failure classes describe the logging component's own problem.
# gRPC status codes (both spellings) and POSIX socket errors are taken from their specifications.
LOG_CLASSES: list[tuple[str, re.Pattern[str]]] = [
    (
        "authentication failure",
        re.compile(
            r"(?i)(NOAUTH|WRONGPASS|authentication (?:failed|required|error)|invalid (?:password|credentials)|"
            r"access denied|password authentication failed|\b401\b|Unauthorized|ERR AUTH|SASL|auth failed|"
            r"\bUNAUTHENTICATED\b)"
        ),
    ),
    (
        "authorization denied (RBAC)",
        re.compile(
            r"(?i)(is forbidden|\bforbidden\b|\b403\b|cannot (?:get|list|watch|create|update|patch|delete) resource|"
            r"\bRBAC\b|not allowed to|PermissionDenied|\bPERMISSION_DENIED\b)"
        ),
    ),
    (
        "DNS resolution failure",
        re.compile(
            r"(?i)(no such host|NXDOMAIN|name resolution|could not resolve|getaddrinfo|server misbehaving|"
            r"Temporary failure in name|lookup [^\s:]+(?::\d+)? on [0-9.]+|dns.*(?:fail|error)|EAI_AGAIN|ENOTFOUND|"
            r"resolve host)"
        ),
    ),
    (
        "message or data processing failure",
        re.compile(
            r"(?i)(deserializ|serialization ?(?:exception|error)|poison|malformed|corrupt|unmarshal|failed to parse|"
            r"parse error|invalid (?:message|payload|record|json|format)|decod(?:e|ing) (?:error|failed)|"
            r"SchemaException|\bDATA_LOSS\b|\bDataLoss\b)"
        ),
    ),
    ("out of memory", re.compile(r"(?i)(out of memory|OOMKilled|\bOOM\b|Cannot allocate memory|heap)")),
    ("filesystem permission denied", re.compile(r"(?i)(permission denied|EACCES|read-only file system|EROFS)")),
    (
        "overload or quota exhaustion",
        re.compile(r"(\bRESOURCE_EXHAUSTED\b|\bResourceExhausted\b|(?i:too many requests|\b429\b|rate limit))"),
    ),
    (
        "connectivity or timeout",
        re.compile(
            r"(?i)(connection refused|dial tcp|timed? ?out|deadline exceeded|\bUNAVAILABLE\b|reset by peer|"
            r"broken pipe|no route to host|\bEOF\b|\b50[234]\b|i/o timeout|unreachable|ECONNREFUSED|ETIMEDOUT|"
            r"ECONNRESET|EHOSTUNREACH|ENETUNREACH|ECONNABORTED|EPIPE|connect: |retry|circuit|"
            r"\bDEADLINE_EXCEEDED\b|\bDeadlineExceeded\b|\bCANCELLED\b|\bCanceled\b|" + _CONNECT_FAILURE + ")"
        ),
    ),
]
# Failures of the telemetry pipeline (trace/metric/log export) never break application requests; they
# are classified so they can be reported without creating pointers or own-failure signals.
TELEMETRY_RE = re.compile(
    r"(?i)(opentelemetry|otel[-_ .:/]|otel-collector|otlp|jaeger|zipkin|\btempo\b|exporter\.internal|"
    r"(?:span|metric|log|trace)s? ?export(?:er|ing)?\b|exporter export|export(?:ing)? (?:spans|metrics|logs|traces)|"
    r"HttpExporter|telemetry|prometheus remote|pushgateway|kubeletstats|scraper)"
)
DEPENDENCY_CLASSES = {
    "connectivity or timeout",
    "DNS resolution failure",
    "authentication failure",
    "overload or quota exhaustion",
}
OWN_FAILURE_CLASSES = {
    "authorization denied (RBAC)",
    "message or data processing failure",
    "out of memory",
    "filesystem permission denied",
    "authentication failure",
}

# A server logging that it refused a login, in the formats of common servers, keyed by the backend role that
# writes them. The account is the one the client presented; the server is reporting on its client, not failing
# itself. Clients often relay the server's message through their driver, so a line counts only when the component
# logging it plays that server role.
LOGIN_REJECTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("postgresql", re.compile(r'password authentication failed for user "(?P<account>[^"]+)"')),
    ("postgresql", re.compile(r'no pg_hba\.conf entry for host "[^"]*", user "(?P<account>[^"]+)"')),
    ("postgresql", re.compile(r'FATAL:\s+role "(?P<account>[^"]+)" does not exist')),
    ("mysql", re.compile(r"Access denied for user '(?P<account>[^']+)'@")),
    ("mongodb", re.compile(r'(?i)authentication failed.{0,200}?"(?:user|principalName)"\s*:\s*"(?P<account>[^"]+)"')),
    ("rabbitmq", re.compile(r"user '(?P<account>[^']+)' - invalid credentials")),
    ("rabbitmq", re.compile(r"HTTP access denied: user '(?P<account>[^']+)'")),
)

ANOMALOUS_FLAG_PREFIXES = ("pod spec sets hostAliases", "dnsPolicy is", "custom dnsConfig", "hostNetwork enabled")

# Backends that are drop-in replacements for one another: a client's error names the protocol ("redis"), the
# workload is named after the implementation ("valkey"). Matching either way is protocol knowledge, not tuning.
ROLE_SYNONYMS: list[set[str]] = [
    {"redis", "valkey", "keydb", "dragonfly"},
    {"mongodb", "mongo", "mongos", "documentdb"},
    {"postgresql", "postgres", "pgsql", "psql"},
    {"mysql", "mariadb", "percona"},
    {"elasticsearch", "opensearch", "elastic"},
    {"kafka", "redpanda", "broker"},
    {"rabbitmq", "amqp"},
    {"memcached", "memcache"},
    {"zookeeper", "zk"},
    {"jaeger", "tempo", "zipkin"},
]
ROLE_NAMES = (
    "redis",
    "mongodb",
    "postgresql",
    "mysql",
    "elasticsearch",
    "kafka",
    "rabbitmq",
    "memcached",
    "zookeeper",
    "tracing",
)
DATABASE_ROLES = {"postgresql", "mysql", "mongodb"}
# Env var name tokens that denote a database connection, for linking a client to the application's only database.
_DATABASE_ENV_TOKENS = {
    "db",
    "database",
    "dsn",
    "jdbc",
    "sql",
    "postgres",
    "postgresql",
    "pg",
    "mysql",
    "mongo",
    "mongodb",
}
# A name followed by one of these words still refers to the named component ("user-service-client").
GENERIC_NAME_SUFFIXES = {
    "client",
    "clients",
    "svc",
    "service",
    "server",
    "srv",
    "grpc",
    "http",
    "https",
    "api",
    "rpc",
    "conn",
    "connection",
    "pool",
    "primary",
    "master",
    "leader",
    "headless",
    "internal",
    "cluster",
    "proxy",
}
# Rejections that began within this window after a credential source was rewritten are attributed to it.
REJECTION_CORRELATION_SECONDS = 300


def _component_tokens(comp: dict) -> set[str]:
    tokens = {t for t in re.split(r"[-_./]", comp["name"].lower()) if len(t) >= 3}
    for c in comp.get("containers") or []:
        image = (c.get("image") or "").split("@")[0].split(":")[0]
        for part in image.split("/")[-2:]:
            tokens |= {t for t in re.split(r"[-_.]", part.lower()) if len(t) >= 3}
    return tokens


def backend_role(comp: dict) -> str | None:
    """The backend role a component plays, from its name and image (redis, postgresql, kafka, ...)."""
    tokens = _component_tokens(comp)
    for name, group in zip(ROLE_NAMES, ROLE_SYNONYMS, strict=True):
        if tokens & (group - {"broker"}):
            return name
    return None


def _name_aliases(components: dict[str, dict]) -> dict[str, str]:
    """Lowercased names that identify a component in log text: its own name, its Services' names, and, when
    exactly one component in the application plays a backend role, the synonyms of that role."""
    aliases: dict[str, str] = {}
    for cid, comp in components.items():
        aliases.setdefault(comp["name"].lower(), cid)
        for svc in comp.get("services") or []:
            if svc.get("name"):
                aliases.setdefault(svc["name"].lower(), cid)
    tokens = {cid: _component_tokens(comp) for cid, comp in components.items()}
    for group in ROLE_SYNONYMS:
        players = [cid for cid, toks in tokens.items() if toks & group]
        if len(players) == 1:
            for word in group:
                aliases.setdefault(word, players[0])
    return aliases


def _mentions(text: str, aliases: dict[str, str], exclude: str) -> set[str]:
    """Components whose name appears in `text` as a whole token (so `cart` does not match `valkey-cart`).

    A name followed by a generic word ("user-service-client") still counts, unless the longer name belongs to
    another component.
    """
    found = set()
    lowered = text.lower()
    for alias, cid in aliases.items():
        if cid == exclude or len(alias) < 3:
            continue
        pattern = rf"(?<![a-z0-9_-]){re.escape(alias)}(?:-(?P<suffix>[a-z0-9]+)(?![a-z0-9_-])|(?![a-z0-9_-]))"
        for m in re.finditer(pattern, lowered):
            suffix = m.group("suffix")
            if suffix is None or (suffix in GENERIC_NAME_SUFFIXES and f"{alias}-{suffix}" not in aliases):
                found.add(cid)
                break
    return found


def _login_rejection(text: str, role: str | None) -> str | None:
    """The rejected account, when `text` is a login rejection logged by a server of the component's own role."""
    if role is None:
        return None
    for server_role, rx in LOGIN_REJECTIONS:
        if server_role == role:
            m = rx.search(text)
            if m:
                return m.group("account")
    return None


def classify_logs(components: dict[str, dict]) -> None:
    """Turn error-log samples that are still occurring into typed findings and cross-component pointers."""
    aliases = _name_aliases(components)
    pointers: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))  # target -> source -> class
    examples: dict[tuple[str, str], str] = {}
    observability = {cid for cid, comp in components.items() if OBSERVABILITY_RE.search(comp["name"])}
    for cid, comp in components.items():
        for key in ("log_findings", "errors_point_to", "referenced_by_errors_from", "telemetry_error_lines"):
            comp.pop(key, None)
        role = backend_role(comp)
        findings: list[dict[str, Any]] = []
        telemetry_lines = ongoing_lines = 0
        for sig in comp.get("log_signals") or []:
            if sig.get("state") == "stopped":
                continue
            line = sig.get("line") or ""
            count = int(sig.get("count") or 1)
            body = line.split(": ", 1)[1] if ": " in line[:40] else line  # drop the "container: " prefix
            if bool(TELEMETRY_RE.search(body)) and cid not in observability:
                telemetry_lines += count
                findings.append(
                    {"classes": ["telemetry export failure"], "count": count, "targets": [], "line": line[:200]}
                )
                continue
            ongoing_lines += count
            if _login_rejection(body, role):
                findings.append(
                    {"classes": ["login rejected by this server"], "count": count, "targets": [], "line": line[:200]}
                )
                continue
            classes = [name for name, rx in LOG_CLASSES if rx.search(body)]
            targets = {t for t in _mentions(body, aliases, exclude=cid) if t not in observability}
            if not classes and not targets:
                continue
            findings.append({"classes": classes, "count": count, "targets": sorted(targets), "line": line[:200]})
            for target in targets:
                for klass in classes or ["error mentioning the component"]:
                    pointers[target][cid][klass] += count
                    examples.setdefault((target, cid), body[:160])
        if telemetry_lines:
            comp["telemetry_error_lines"] = telemetry_lines
        if comp.get("log_signals") is not None:
            comp["log_error_lines"] = ongoing_lines or None
        if findings:
            comp["log_findings"] = findings
            own = Counter()
            own_examples: dict[str, str] = {}
            for f in findings:
                for klass in f["classes"]:
                    if klass in OWN_FAILURE_CLASSES:
                        own[klass] += f["count"]
                        own_examples.setdefault(klass, f["line"])
            for klass, n in own.most_common():
                where = ""
                targets = sorted({t for f in findings if klass in f["classes"] for t in f["targets"]})
                if targets:
                    where = f" when calling {', '.join(targets)}"
                comp["signals"].append(f"logs report {klass}{where} ({n} lines): {own_examples[klass][:140]}")
            pointing_at = sorted({t for f in findings for t in f["targets"]})
            if pointing_at:
                comp["errors_point_to"] = pointing_at
    for target, sources in pointers.items():
        comp = components[target]
        comp["referenced_by_errors_from"] = {src: dict(classes) for src, classes in sources.items()}
        names = ", ".join(sorted(sources))
        total = sum(sum(c.values()) for c in sources.values())
        top_class = Counter()
        for c in sources.values():
            top_class.update(c)
        klass = top_class.most_common(1)[0][0]
        example = next(iter(v for (t, _), v in examples.items() if t == target), "")
        comp["signals"].append(
            f"errors logged by {len(sources)} other component(s) ({names}) name this component "
            f"({klass}, {total} lines, still occurring), e.g. '{example}'"
        )


def server_login_rejections(components: dict[str, dict], collected_at: datetime) -> None:
    """A server that rejects logins points at the clients that present the rejected credentials.

    Candidate clients are scored. Strongest: a Secret or ConfigMap the client reads through env values was
    rewritten shortly before the rejections began and after the client's containers started, so the running
    client still holds the old value. A literal env value naming the rejected account identifies the client only
    when no other client names it too; clients usually share an account. A client whose latest pod template
    updated the env value holding the account, and whose pods all started after that update, runs with the new
    value and is weakened. The client's env values addressing the server, or its env var names denoting a
    database connection when the server is the application's only database, add a little. Secret values are
    never read.
    """
    databases = [cid for cid, comp in components.items() if backend_role(comp) in DATABASE_ROLES]
    for sid, server in components.items():
        server.pop("rejected_accounts", None)
        server.pop("login_candidates", None)
        role = backend_role(server)
        accounts: Counter[str] = Counter()
        began = None
        for sig in server.get("log_signals") or []:
            if sig.get("state") == "stopped":
                continue
            account = _login_rejection(sig.get("line") or "", role)
            if account:
                accounts[account] += int(sig.get("count") or 1)
                if sig.get("first_seen_seconds_ago") is not None:
                    start = collected_at - timedelta(seconds=sig["first_seen_seconds_ago"])
                    began = start if began is None or start < began else began
        if not accounts:
            continue
        server["rejected_accounts"] = dict(accounts)
        names = ", ".join(sorted(accounts))
        total = sum(accounts.values())
        naming = {
            cid
            for cid, comp in components.items()
            if cid != sid and any(acc in v for acc in accounts for v in comp.get("_env_values") or [])
        }
        candidates = []
        for cid, comp in components.items():
            if cid == sid or comp.get("role") == "observability":
                continue
            reasons: list[str] = []
            score = 0
            if cid in naming:
                if len(naming) == 1:
                    reasons.append(f"it is the only client whose env values name the account {names}")
                    score += 3
                else:
                    reasons.append(f"its env values name the account {names}, as do {len(naming) - 1} other client(s)")
                    score += 1
            tc = comp.get("template_changes") or {}
            changed_at = parse_time(tc.get("changed_at"))
            starts = [parse_time(p.get("started")) for p in comp.get("pods") or []]
            refreshed = any(
                " env " in ch and "->" in ch and any(acc in ch.split("->", 1)[1] for acc in accounts)
                for ch in tc.get("changes") or []
            )
            if refreshed and changed_at and starts and all(t and t >= changed_at for t in starts):
                reasons.append(
                    "its latest pod template updated the env value holding that account and its pods started after the update"
                )
                score -= 2
            if began is not None:
                for ref, obj in (comp.get("config_objects") or {}).items():
                    written = parse_time(obj.get("last_update"))
                    fixed_read = any(
                        "fixed when the container starts" in s and ref.split("/", 1)[1] in s
                        for s in comp.get("signals") or []
                    )
                    if (
                        written
                        and fixed_read
                        and 0 <= (began - written).total_seconds() <= REJECTION_CORRELATION_SECONDS
                    ):
                        reasons.append(
                            f"{ref}, which it reads through env values, was rewritten "
                            f"{int((began - written).total_seconds())}s before the rejections began and after its "
                            "containers started"
                        )
                        score += 3
                        break
            if sid in (comp.get("_calls") or []):
                reasons.append(f"its env values address {server['name']}")
                score += 1
            elif databases == [sid]:
                env_names = [n for c in comp.get("containers") or [] for n in c.get("env_names") or []]
                db_vars = [n for n in env_names if set(n.lower().split("_")) & _DATABASE_ENV_TOKENS]
                if db_vars:
                    reasons.append(
                        f"its env var {db_vars[0]} names a database connection and {server['name']} is the "
                        "application's only database"
                    )
                    score += 1
            if reasons and score > 0:
                candidates.append((score, cid, reasons))
        candidates.sort(key=lambda c: (-c[0], c[1]))
        top = candidates[:3]
        if top:
            server["login_candidates"] = [cid for _, cid, _ in top]
        listed = "; ".join(f"{cid} ({', '.join(r)})" for _, cid, r in top)
        server["signals"].append(
            f"rejects logins for account {names} ({total} lines, still occurring); the rejected credentials come "
            f"from its clients" + (f": candidates {listed}" if top else "")
        )
        for _, cid, reasons in top:
            components[cid]["signals"].append(
                f"server {sid} rejects logins for account {names} ({total} lines, still occurring); this component "
                f"may present those credentials: {'; '.join(reasons)}"
            )


def promote_anomalous_flags(components: dict[str, dict]) -> None:
    """Settings that break in-cluster name resolution or networking are signals, not footnotes."""
    for comp in components.values():
        keep = []
        for flag in comp.get("spec_flags") or []:
            if flag.startswith(ANOMALOUS_FLAG_PREFIXES):
                comp["signals"].append(f"pod spec: {flag}")
            else:
                keep.append(flag)
        comp["spec_flags"] = keep


def flag_service_fanout(components: dict[str, dict]) -> None:
    """A Service that selects pods of more than one workload has a selector broader than one workload.

    When the collector kept the selector and each workload's container ports, the selector is quoted and every
    selected workload that does not expose the Service's targetPort is named.
    """
    owners: dict[tuple[str, str], list[str]] = defaultdict(list)
    records: dict[tuple[str, str], dict] = {}
    for cid, comp in components.items():
        for svc in comp.get("services") or []:
            owners[(comp["namespace"], svc["name"])].append(cid)
            records.setdefault((comp["namespace"], svc["name"]), svc)
    for key, cids in owners.items():
        if len(cids) < 2:
            continue
        name, record = key[1], records[key]
        ports = record.get("workload_ports") or {}
        targets = [p.split("->")[1].split("/")[0] for p in record.get("ports") or [] if "->" in p]
        unexposed = [
            f"{c} does not expose targetPort {t} (its containers expose {ports.get(c) or 'no ports'})"
            for c in cids
            for t in targets
            if t.isdigit() and c in ports and int(t) not in (ports.get(c) or [])
        ]
        selector = f" through selector {record['selector']}" if record.get("selector") else ""
        for cid in cids:
            others = [c for c in cids if c != cid]
            text = (
                f"service {name} selects pods of {len(cids)} different workloads (also {', '.join(others)}){selector}; "
                "its selector is broader than this workload, so traffic is spread across unrelated pods"
            )
            if unexposed:
                text += "; " + "; ".join(unexposed)
            components[cid]["signals"].append(text)
            for svc in components[cid].get("services") or []:
                if svc["name"] == name:
                    svc["selects_multiple_workloads"] = cids


def apply_quota_requirements(components: dict[str, dict], constraints: list[dict]) -> None:
    """A ResourceQuota with a hard limit on a resource requires every pod to declare it."""
    for c in constraints:
        if c.get("kind") != "ResourceQuota":
            continue
        required = {res.split(".")[-1] for res in (c.get("hard") or {}) if res.split(".")[-1] in ("memory", "cpu")}
        if not required:
            continue
        rejected = []
        for cid, comp in components.items():
            if comp["namespace"] != c["namespace"]:
                continue
            missing = sorted(
                res
                for res in required
                if any(
                    not (ct.get("limits") or {}).get(res) and not (ct.get("requests") or {}).get(res)
                    for ct in comp.get("containers") or []
                    if not ct.get("init")
                )
            )
            quota_events = [
                e for e in comp.get("warning_events") or [] if "failed quota" in (e.get("message") or "").lower()
            ]
            short = (comp.get("replicas") or {}).get("desired", 0) > len(comp.get("pods") or [])
            if missing and (quota_events or short):
                comp["signals"].append(
                    f"ResourceQuota {c['name']} requires {', '.join(missing)} on every pod and this workload's "
                    f"containers declare none, so the API server rejects its new pods"
                )
                rejected.append(cid)
        signal = f"rejects pods of {', '.join(rejected)} (missing {', '.join(sorted(required))})" if rejected else None
        c["signals"] = [s for s in c.get("signals") or [] if not s.startswith("rejects pods of ")]
        if signal:
            c["signals"].append(signal)
            c["components"] = rejected


def retire_stale_events(components: dict[str, dict]) -> None:
    """Warning events about pods that no longer exist, on a workload whose pods are all ready, are history."""
    for comp in components.values():
        pods = comp.get("pods") or []
        current = {p.get("name") for p in pods}
        all_ready = bool(pods) and all(
            p.get("ready", "0/0").split("/")[0] == p.get("ready", "0/0").split("/")[1] for p in pods
        )
        keep, stale = [], []
        for ev in comp.get("warning_events") or []:
            obj = ev.get("object") or ""
            gone = obj.startswith("pod/") and obj[4:] not in current
            if gone and (all_ready or "does not exist any more" in (ev.get("message") or "")):
                stale.append(ev)
            else:
                keep.append(ev)
        if not stale:
            continue
        comp["warning_events"] = keep
        comp.setdefault("startup_events", []).extend(stale[:3])
        comp["signals"] = [s for s in comp["signals"] if not s.startswith("warning events:")]
        if keep:
            reasons: Counter[str] = Counter()
            for ev in keep:
                reasons[ev["reason"]] += int(ev.get("count") or 1)
            comp["signals"].append("warning events: " + ", ".join(f"{r} x{n}" for r, n in reasons.most_common(4)))


def tag_roles(components: dict[str, dict]) -> None:
    for comp in components.values():
        if OBSERVABILITY_RE.search(comp["name"]):
            comp["role"] = "observability"


SYMPTOM_MARKERS = (
    "desired replicas are ready",
    "pod phase",
    "is terminating",
    "restarted",
    "running but not ready",
    "waiting in",
    "terminated",
    "warning events:",
    "at its limit",
    "near its limit",
    "no pods exist",
    "was OOMKilled",
    "unschedulable",
    "more time(s) in the last",
    "rejects logins for account",
)
CHANGE_MARKERS = ("spec modified", "was modified", "was written", "was created", "created ", "pod template changed")
POINTED_MARKERS = ("errors logged by", "server ")
OWN_LOG_MARKERS = ("logs report", "Kafka consumer group")


def classify_evidence(components: dict[str, dict]) -> None:
    """Tag each component with the kinds of evidence it carries, so the triage rules apply literally.

    change: its own spec, Service, Secret, ConfigMap, policy, or role was written or created as a separate change.
    configuration: an abnormal setting of its own (policy, selector, probe, env, mount, DNS, rollout, job, quota).
    pointed_at: other components' still-occurring errors name it, or a server rejects credentials it may present.
    own_failure_logs: its logs show authorization, data, memory, or filesystem failures, or its consumer stalls.
    symptoms: pod/replica/resource state only.
    """
    for comp in components.values():
        kinds: set[str] = set()
        for sig in comp.get("signals") or []:
            if any(m in sig for m in CHANGE_MARKERS):
                kinds.add("change")
            elif sig.startswith(POINTED_MARKERS):
                kinds.add("pointed_at")
            elif sig.startswith(OWN_LOG_MARKERS):
                kinds.add("own_failure_logs")
            elif any(m in sig for m in SYMPTOM_MARKERS):
                kinds.add("symptoms")
            else:
                kinds.add("configuration")
        if comp.get("log_error_lines") and not kinds:
            kinds.add("error_logs_only")
        comp["evidence_kinds"] = sorted(kinds)


def cluster_findings(cluster: dict, ranked_changes: list[dict]) -> list[dict]:
    """Cluster-scoped and namespace-scoped objects with a computed signal, for the `other` path.

    Each finding carries `components`: the workloads the object is known to act on (the pods a webhook
    rewrote, the pods a quota rejected, the workloads a policy selects or a role is bound to).
    """
    out: list[dict] = []
    for wh in cluster.get("admission_webhooks") or []:
        for sig in wh.get("signals") or []:
            out.append(
                {
                    "kind": wh.get("kind"),
                    "name": wh.get("configuration"),
                    "signal": sig,
                    "components": wh.get("components") or [],
                }
            )
    for c in cluster.get("namespace_constraints") or []:
        for sig in c.get("signals") or []:
            out.append(
                {
                    "kind": c.get("kind"),
                    "name": c.get("name"),
                    "namespace": c.get("namespace"),
                    "signal": sig,
                    "components": c.get("components") or [],
                }
            )
    for pol in cluster.get("network_policies") or []:
        if pol.get("selects_all_pods") or pol.get("changed"):
            out.append(
                {
                    "kind": "NetworkPolicy",
                    "name": pol.get("name"),
                    "namespace": pol.get("namespace"),
                    "signal": pol.get("effect"),
                    "components": pol.get("components") or [],
                }
            )
    for svc in cluster.get("services_matching_no_workload") or []:
        out.append(
            {
                "kind": "Service",
                "name": svc.get("name"),
                "signal": f"selector {svc.get('selector')} matches no workload",
            }
        )
    for node in cluster.get("nodes") or []:
        for p in node.get("problems") or []:
            out.append({"kind": "Node", "name": node.get("name"), "signal": p})
    dns = cluster.get("cluster_dns") or {}
    for sig in dns.get("signals") or []:
        out.append(
            {
                "kind": "ConfigMap",
                "name": "coredns",
                "namespace": "kube-system",
                "signal": sig,
                "components": dns.get("components") or [],
            }
        )
    for role in cluster.get("rbac") or []:
        if role.get("signals"):
            out.append(
                {
                    "kind": role.get("kind"),
                    "name": role.get("name"),
                    "namespace": role.get("namespace"),
                    "signal": "; ".join(role["signals"])
                    + f"; bound via {', '.join(role.get('bindings') or [])} to {', '.join(role.get('components') or [])}",
                    "components": role.get("components") or [],
                }
            )
    for obj in cluster.get("stuck_terminating") or []:
        out.append(
            {
                "kind": obj["kind"],
                "name": obj["name"],
                "namespace": obj.get("namespace"),
                "signal": f"terminating for {obj.get('terminating_for_s')}s, held by finalizer {', '.join(obj['finalizers'])}",
                "components": obj.get("components") or [],
            }
        )
    for ch in ranked_changes:
        if ch["kind"] in ("Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job"):
            continue
        text = change_log.describe_change(ch)
        existing = next((f for f in out if f.get("kind") == ch["kind"] and f.get("name") == ch["name"]), None)
        if existing is not None:
            existing["signal"] = f"{existing['signal']}; {text}"
            continue
        out.append(
            {
                "kind": ch["kind"],
                "name": ch["name"],
                "namespace": ch.get("namespace"),
                "signal": text,
                "components": ch.get("affects") or [],
            }
        )
    seen: set[tuple] = set()
    unique: list[dict] = []
    for f in out:
        key = (f.get("kind"), f.get("name"), f.get("namespace"), f.get("signal"))
        if key not in seen:
            seen.add(key)
            unique.append({k: v for k, v in f.items() if v not in (None, [])})
    return unique


# Signal prefixes derived by an earlier version of this module; removed when re-deriving an old saved snapshot.
_LEGACY_DERIVED_PREFIXES = ("errors logged by", "logs report", "ResourceQuota ", "pod spec: ", "referenced ")


def post_process(snapshot) -> None:
    """Apply every derivation in place. Safe to run again on the same snapshot or on a saved snapshot.

    Signals added here are remembered in `_derived_signals` and removed before the next run, so re-running
    after new evidence arrives (an exec probe, for example) replaces them instead of duplicating them.
    """
    components = snapshot.components
    collected_at = parse_time(getattr(snapshot, "collected_at", None)) or now()
    base: dict[str, list[str]] = {}
    for comp in components.values():
        signals = comp.setdefault("signals", [])
        if "_derived_signals" in comp:
            derived = set(comp["_derived_signals"])
            signals = [s for s in signals if s not in derived]
        elif comp.get("_derived"):
            signals = [s for s in signals if not s.startswith(_LEGACY_DERIVED_PREFIXES) and "selects pods of" not in s]
        comp["signals"] = signals
        for flag in ("nearest_change_before_symptoms", "most_recent_change"):
            comp.pop(flag, None)
    # Transformations of the collected data itself: their results are kept as collected signals.
    promote_anomalous_flags(components)
    retire_stale_events(components)
    for cid, comp in components.items():
        base[cid] = list(comp["signals"])
    flag_service_fanout(components)
    apply_quota_requirements(components, snapshot.cluster.get("namespace_constraints") or [])
    tag_roles(components)
    classify_logs(components)
    server_login_rejections(components, collected_at)
    ranked = change_log.rank_changes(components, snapshot.cluster, collected_at)
    snapshot.cluster["findings"] = cluster_findings(snapshot.cluster, ranked)
    for cid, comp in components.items():
        comp["signals"] = list(dict.fromkeys(comp["signals"]))
        kept = set(base.get(cid, []))
        comp["_derived_signals"] = [s for s in comp["signals"] if s not in kept]
        comp["healthy"] = not comp["signals"] and not comp.get("log_error_lines")
        comp["_derived"] = True
    classify_evidence(components)
