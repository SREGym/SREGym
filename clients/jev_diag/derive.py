"""Derived signals computed in code over the compact component records.

Everything here is deterministic post-processing of what the collector already
gathered. It runs at the end of a live collection and, identically, over a saved
snapshot (see replay.py), so question design can be evaluated offline.

The derivations encode general SRE triage knowledge, not knowledge of any
particular fault:

* Error logs are classified into semantic classes (authentication, RBAC denial,
  DNS, data corruption, connectivity) and the component each error names is
  resolved, so a component that other components' errors point at receives a
  structural signal.
* A Service whose selector matches several workloads, a ResourceQuota that
  rejects pods for missing fields, and spec settings that break in-cluster
  networking become signals instead of buried detail.
* Warning events about pods that no longer exist are history, not evidence.
* Observability components are tagged so the model can apply the right prior.
* Objects written after they were created are the "what changed" list.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

MIN_SPEC_CHANGE_SECONDS = 10  # a write this long after creation is a change, not part of the rollout

OBSERVABILITY_RE = re.compile(
    r"(grafana|jaeger|prometheus|otel|opentelemetry|opensearch|elasticsearch|kibana|loki|fluent|promtail|"
    r"alertmanager|kube-state-metrics|node-exporter|tempo|zipkin|pushgateway|blackbox)",
    re.I,
)

# Semantic classes of error lines. Dependency classes describe a failed call to another component and
# therefore point at that component; own-failure classes describe the logging component's own problem.
LOG_CLASSES: list[tuple[str, re.Pattern[str]]] = [
    (
        "authentication failure",
        re.compile(
            r"(?i)(NOAUTH|WRONGPASS|authentication (?:failed|required|error)|invalid (?:password|credentials)|"
            r"access denied|password authentication failed|\b401\b|Unauthorized|ERR AUTH|SASL|auth failed)"
        ),
    ),
    (
        "authorization denied (RBAC)",
        re.compile(
            r"(?i)(is forbidden|\bforbidden\b|\b403\b|cannot (?:get|list|watch|create|update|patch|delete) resource|"
            r"\bRBAC\b|not allowed to|PermissionDenied)"
        ),
    ),
    (
        "DNS resolution failure",
        re.compile(
            r"(?i)(no such host|NXDOMAIN|name resolution|could not resolve|getaddrinfo|server misbehaving|"
            r"Temporary failure in name|lookup [^\s:]+(?::\d+)? on [0-9.]+|dns.*(?:fail|error)|EAI_AGAIN|ENOTFOUND)"
        ),
    ),
    (
        "message or data processing failure",
        re.compile(
            r"(?i)(deserializ|serialization ?(?:exception|error)|poison|malformed|corrupt|unmarshal|failed to parse|"
            r"parse error|invalid (?:message|payload|record|json|format)|decod(?:e|ing) (?:error|failed)|SchemaException)"
        ),
    ),
    ("out of memory", re.compile(r"(?i)(out of memory|OOMKilled|\bOOM\b|Cannot allocate memory|heap)")),
    ("filesystem permission denied", re.compile(r"(?i)(permission denied|EACCES|read-only file system|EROFS)")),
    (
        "connectivity or timeout",
        re.compile(
            r"(?i)(connection refused|dial tcp|timed? ?out|deadline exceeded|\bUNAVAILABLE\b|reset by peer|"
            r"broken pipe|no route to host|\bEOF\b|\b50[234]\b|i/o timeout|unreachable|ECONNREFUSED|ETIMEDOUT|"
            r"connect: |retry|circuit)"
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
DEPENDENCY_CLASSES = {"connectivity or timeout", "DNS resolution failure", "authentication failure"}
OWN_FAILURE_CLASSES = {
    "authorization denied (RBAC)",
    "message or data processing failure",
    "out of memory",
    "filesystem permission denied",
    "authentication failure",
}

ANOMALOUS_FLAG_PREFIXES = ("pod spec sets hostAliases", "dnsPolicy is", "custom dnsConfig", "hostNetwork enabled")


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


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


def _component_tokens(comp: dict) -> set[str]:
    tokens = {t for t in re.split(r"[-_./]", comp["name"].lower()) if len(t) >= 3}
    for c in comp.get("containers") or []:
        image = (c.get("image") or "").split("@")[0].split(":")[0]
        for part in image.split("/")[-2:]:
            tokens |= {t for t in re.split(r"[-_.]", part.lower()) if len(t) >= 3}
    return tokens


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
    """Components whose name appears in `text` as a whole token (so `cart` does not match `valkey-cart`)."""
    found = set()
    lowered = text.lower()
    for alias, cid in aliases.items():
        if cid == exclude or len(alias) < 3:
            continue
        if re.search(rf"(?<![a-z0-9_-]){re.escape(alias)}(?![a-z0-9_-])", lowered):
            found.add(cid)
    return found


def classify_logs(components: dict[str, dict]) -> None:
    """Turn error-log samples into typed findings and cross-component pointers."""
    aliases = _name_aliases(components)
    pointers: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))  # target -> source -> class
    examples: dict[tuple[str, str], str] = {}
    observability = {cid for cid, comp in components.items() if OBSERVABILITY_RE.search(comp["name"])}
    for cid, comp in components.items():
        findings: list[dict[str, Any]] = []
        telemetry_lines = 0
        for sig in comp.get("log_signals") or []:
            line = sig.get("line") or ""
            count = int(sig.get("count") or 1)
            body = line.split(": ", 1)[1] if ": " in line[:40] else line  # drop the "container: " prefix
            telemetry = bool(TELEMETRY_RE.search(body)) and cid not in observability
            classes = [name for name, rx in LOG_CLASSES if rx.search(body)]
            targets = {t for t in _mentions(body, aliases, exclude=cid) if t not in observability}
            if telemetry:
                telemetry_lines += count
                findings.append(
                    {"classes": ["telemetry export failure"], "count": count, "targets": [], "line": line[:200]}
                )
                continue
            if not classes and not targets:
                continue
            findings.append({"classes": classes, "count": count, "targets": sorted(targets), "line": line[:200]})
            for target in targets:
                for klass in classes or ["error mentioning the component"]:
                    pointers[target][cid][klass] += count
                    examples.setdefault((target, cid), body[:160])
        if telemetry_lines:
            comp["telemetry_error_lines"] = telemetry_lines
            if comp.get("log_error_lines"):
                comp["log_error_lines"] = max(0, comp["log_error_lines"] - telemetry_lines) or None
        if findings:
            comp["log_findings"] = findings
            own = Counter()
            own_examples: dict[str, str] = {}
            for f in findings:
                for klass in f["classes"]:
                    if klass in OWN_FAILURE_CLASSES and "telemetry export failure" not in f["classes"]:
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
            f"({klass}, {total} lines), e.g. '{example}'"
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
    """A Service that selects pods of more than one workload has a selector broader than one workload."""
    owners: dict[tuple[str, str], list[str]] = defaultdict(list)
    for cid, comp in components.items():
        for svc in comp.get("services") or []:
            owners[(comp["namespace"], svc["name"])].append(cid)
    for (_ns, name), cids in owners.items():
        if len(cids) < 2:
            continue
        for cid in cids:
            others = [c for c in cids if c != cid]
            components[cid]["signals"].append(
                f"service {name} selects pods of {len(cids)} different workloads (also {', '.join(others)}); "
                "its selector is broader than this workload, so traffic is spread across unrelated pods"
            )
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
        if rejected:
            c.setdefault("signals", []).append(
                f"rejects pods of {', '.join(rejected)} (missing {', '.join(sorted(required))})"
            )


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


def rank_changes(components: dict[str, dict], cluster: dict) -> None:
    """Objects written well after they were created are the changes made to a running application."""
    now = datetime.now(UTC)
    changed = []
    for cid, comp in components.items():
        created, modified = _parse_time(comp.get("created")), _parse_time(comp.get("modified"))
        if not created or not modified:
            continue
        delta = int((modified - created).total_seconds())
        if delta >= MIN_SPEC_CHANGE_SECONDS:
            ago = max(0, int((now - modified).total_seconds()))
            comp["spec_changed_after_creation_s"] = delta
            if not any(s.startswith("spec modified") for s in comp["signals"]):
                comp["signals"].append(f"spec modified {delta}s after this object was created ({ago}s ago)")
            changed.append(
                (modified, {"object": cid, "modified": comp.get("modified"), "seconds_after_creation": delta})
            )
    existing = cluster.get("recent_changes") or []
    seen = {e["object"] for e in existing}
    merged = existing + [e for _, e in sorted(changed, key=lambda t: t[0], reverse=True) if e["object"] not in seen]
    merged.sort(key=lambda e: e.get("modified") or "", reverse=True)
    for entry in merged:
        comp = components.get(entry.get("object"))
        if comp and comp.get("role"):
            entry["role"] = comp["role"]
    cluster["recent_changes"] = merged[:10]
    if merged:
        newest = merged[0]["object"]
        if newest in components:
            components[newest]["most_recent_change"] = True


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
)
CHANGE_MARKERS = ("spec modified", "was modified", "was written", "created ", "after the application was deployed")
POINTED_MARKERS = ("errors logged by",)
OWN_LOG_MARKERS = ("logs report",)


def classify_evidence(components: dict[str, dict]) -> None:
    """Tag each component with the kinds of evidence it carries, so the triage rules apply literally.

    change: its own spec, Service, Secret, or ConfigMap was written after creation or deploy.
    configuration: an abnormal setting of its own (policy, selector, probe, env, mount, DNS, rollout, job, quota).
    pointed_at: other components' errors name it.  own_failure_logs: its logs show auth/RBAC/data failures.
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


def flag_late_config_objects(components: dict[str, dict]) -> None:
    """A referenced Secret or ConfigMap written well after the workloads were created is a change."""
    created = sorted(t for t in (_parse_time(c.get("created")) for c in components.values()) if t)
    if not created:
        return
    baseline = created[len(created) // 2]
    for comp in components.values():
        for ref, obj in (comp.get("config_objects") or {}).items():
            written = _parse_time(obj.get("last_update"))
            if not written:
                continue
            delta = int((written - baseline).total_seconds())
            kind, name = ref.split("/", 1)
            already = any(name in sig and ("modified" in sig or "written" in sig) for sig in comp["signals"])
            if delta >= MIN_SPEC_CHANGE_SECONDS and not already:
                comp["signals"].append(
                    f"referenced {kind} {name} was written {delta}s after the application's workloads were created"
                )


def cluster_findings(cluster: dict) -> list[dict]:
    """Cluster-scoped and namespace-scoped objects with a computed signal, for the `other` path."""
    out: list[dict] = []
    for wh in cluster.get("admission_webhooks") or []:
        for sig in wh.get("signals") or []:
            out.append({"kind": wh.get("kind"), "name": wh.get("configuration"), "signal": sig})
    for c in cluster.get("namespace_constraints") or []:
        for sig in c.get("signals") or []:
            out.append({"kind": c.get("kind"), "name": c.get("name"), "namespace": c.get("namespace"), "signal": sig})
    for pol in cluster.get("network_policies") or []:
        if pol.get("selects_all_pods") or pol.get("seconds_after_deploy"):
            late = f", created {pol['seconds_after_deploy']}s after deploy" if pol.get("seconds_after_deploy") else ""
            out.append(
                {
                    "kind": "NetworkPolicy",
                    "name": pol.get("name"),
                    "namespace": pol.get("namespace"),
                    "signal": f"{pol.get('effect')}{late}",
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
        out.append({"kind": "ConfigMap", "name": "coredns", "namespace": "kube-system", "signal": sig})
    for role in cluster.get("rbac") or []:
        if role.get("signals"):
            out.append(
                {
                    "kind": role.get("kind"),
                    "name": role.get("name"),
                    "namespace": role.get("namespace"),
                    "signal": "; ".join(role["signals"])
                    + f"; bound via {', '.join(role.get('bindings') or [])} to {', '.join(role.get('components') or [])}",
                }
            )
    for ch in cluster.get("recent_changes") or []:
        if "/" in ch["object"] and ch["object"].split("/")[0] not in (
            "deployment",
            "statefulset",
            "daemonset",
            "cronjob",
            "job",
            "role",
            "clusterrole",
        ):
            out.append(
                {
                    "kind": ch["object"].split("/")[0],
                    "name": ch["object"].split("/")[-1],
                    "signal": f"written after the application was deployed ({ch.get('modified')})",
                }
            )
    seen: set[tuple] = set()
    unique: list[dict] = []
    for f in out:
        key = (f.get("kind"), f.get("name"), f.get("namespace"), f.get("signal"))
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def post_process(snapshot) -> None:
    """Apply every derivation in place. Idempotent enough to run on a saved snapshot."""
    components = snapshot.components
    for comp in components.values():
        comp.setdefault("signals", [])
        if comp.get("_derived"):
            comp["signals"] = [
                x
                for x in comp["signals"]
                if not x.startswith(("errors logged by", "logs report", "ResourceQuota ", "pod spec: ", "referenced "))
                and "selects pods of" not in x
            ]
    promote_anomalous_flags(components)
    retire_stale_events(components)
    flag_service_fanout(components)
    apply_quota_requirements(components, snapshot.cluster.get("namespace_constraints") or [])
    classify_logs(components)
    tag_roles(components)
    rank_changes(components, snapshot.cluster)
    flag_late_config_objects(components)
    snapshot.cluster["findings"] = cluster_findings(snapshot.cluster)
    for comp in components.values():
        comp["signals"] = list(dict.fromkeys(comp["signals"]))
        comp["healthy"] = not comp["signals"] and not comp.get("log_error_lines")
        comp["_derived"] = True
    classify_evidence(components)
