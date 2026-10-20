"""Failures of the platform under the application, read from Kubernetes events and node state.

The application's own objects can be healthy while the platform fails them. These failures reach the application
only as event messages, which are read here for their meaning:

* Pod network setup: a pod sandbox that cannot be created because the network plugin or IP address management fails.
* Admission webhook calls that fail when the API server admits the application's objects, with the webhook named
  and how the call fails (certificate, timeout, refused connection, no endpoints), and Pod Security admission that
  rejects its pods under the namespace's enforced level.
* Preemption: the application's pods evicted to make room for pods of higher priority.
* Eviction under node pressure, and the kubelet's own events on the nodes the application runs on.

Every event is read before the per-component event cap, so a platform failure is never crowded out by its symptoms.
"""

from __future__ import annotations

import re
from datetime import datetime

from clients.jev_diag.timeutil import parse_time

_NETWORK_SETUP = re.compile(
    r"(?i)failed to (?:set ?up|setup|create) (?:pod )?(?:network|sandbox)|failed to allocate (?:for range|ip|an ip)|"
    r"no (?:more )?ip addresses? available|ipam|\bcni\b|network plugin|networkplugin"
)
_WEBHOOK_CALL = re.compile(r'failed calling webhook "(?P<name>[^"]+)"')
_CALL_FAILURES = (
    ("its certificate is not trusted or not valid", re.compile(r"(?i)x509|certificate|tls:")),
    ("the call times out", re.compile(r"(?i)deadline exceeded|timeout|timed out")),
    ("its backend refuses the connection", re.compile(r"(?i)connection refused")),
    ("its backend Service has no endpoints", re.compile(r"(?i)no endpoints available|service .* not found")),
)
_NODE_PRESSURE = re.compile(r"(?i)the node (?:was|had) low on resource|node had condition|evicted")
# Kubelet and node events that state a node-level cause (Warning, or a pressure or eviction reason).
NODE_EVENT_REASONS = {
    "EvictionThresholdMet",
    "SystemOOM",
    "NodeHasDiskPressure",
    "NodeHasMemoryPressure",
    "NodeHasInsufficientPID",
    "FreeDiskSpaceFailed",
    "ImageGCFailed",
    "ContainerGCFailed",
    "InvalidDiskCapacity",
    "NodeNotReady",
    "Rebooted",
}


def call_failure(message: str) -> str:
    return next((meaning for meaning, rx in _CALL_FAILURES if rx.search(message)), "the call fails")


def event_signal(reason: str | None, message: str | None) -> str | None:
    """The platform failure a Warning event about an application object states, as a signal; None for any other."""
    reason, message = reason or "", message or ""
    webhook = _WEBHOOK_CALL.search(message)
    if webhook:
        return (
            f"admission webhook {webhook.group('name')} fails when the API server admits this workload's objects: "
            f"{call_failure(message)} ({message[:220]})"
        )
    if "violates PodSecurity" in message:
        return f"the namespace's Pod Security admission rejects this workload's pods: {message[:220]}"
    if reason == "FailedCreatePodSandBox" or (reason.startswith("Failed") and _NETWORK_SETUP.search(message)):
        return f"pod network setup fails (the network plugin or its IP address management): {message[:220]}"
    if reason == "Preempted":
        return f"pods of this workload were preempted to make room for higher-priority pods: {message[:220]}"
    if reason == "Evicted" or (reason.startswith("Evict") and _NODE_PRESSURE.search(message)):
        return f"pods of this workload were evicted under node pressure: {message[:220]}"
    return None


def node_problems(events: list[dict], nodes: list[dict], components: dict[str, dict], since: datetime | None) -> None:
    """Add the kubelet's node events since `since` to each node's problems, and link every node that has problems to
    the components with pods on it."""
    by_name = {n.get("name"): n for n in nodes}
    for ev in events:
        obj = ev.get("involvedObject") or {}
        node = by_name.get(obj.get("name"))
        if node is None or obj.get("kind") != "Node":
            continue
        reason = ev.get("reason") or ""
        if ev.get("type") != "Warning" and reason not in NODE_EVENT_REASONS:
            continue
        last = parse_time(ev.get("lastTimestamp") or ev.get("eventTime"))
        if since and last and last < since:
            continue
        count = ev.get("count") or (ev.get("series") or {}).get("count") or 1
        text = f"node event {reason} x{count}: {(ev.get('message') or '')[:200]}"
        if text not in node.setdefault("problems", []):
            node["problems"].append(text)
    for node in nodes:
        if node.get("problems"):
            node["components"] = sorted(
                cid
                for cid, comp in components.items()
                if any(p.get("node") == node.get("name") for p in comp.get("pods") or [])
            )


def link_webhook_failures(components: dict[str, dict], webhooks: list[dict]) -> None:
    """A webhook whose calls fail for the application's objects carries that failure as its own signal, acting on
    those components (its configuration then becomes a cluster finding for them)."""
    for cid, comp in components.items():
        for sig in comp.get("signals") or []:
            m = re.match(r"admission webhook (\S+) fails when the API server admits this workload's objects: (.+)", sig)
            if not m:
                continue
            for wh in webhooks:
                if wh.get("webhook") != m.group(1):
                    continue
                text = f"calls for the application's objects fail: {m.group(2)[:200]}"
                if text not in wh["signals"]:
                    wh["signals"].append(text)
                wh.setdefault("components", [])
                if cid not in wh["components"]:
                    wh["components"].append(cid)
