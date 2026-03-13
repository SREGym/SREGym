"""
Pre-defined catalog of Chaos Mesh experiment templates.

Each template is a dict with:
  - name: human-readable name for logging
  - kind: Chaos Mesh CRD kind (PodChaos, NetworkChaos, etc.)
  - spec: the CRD spec with {target_namespace} and {duration} placeholders

Only experiments with tangible, observable effects are included.
Removed: IOChaos (volumePath "/" is a no-op), container-kill (redundant
with pod-kill), network-duplicate (too subtle), low-intensity stress tests.
"""

EXPERIMENT_CATALOG = [
    # ── Pod disruptions ──────────────────────────────────────────────
    # Pod-kill: forcefully kills a random pod → visible restart in kubectl
    {
        "name": "pod-kill",
        "kind": "PodChaos",
        "spec": {
            "action": "pod-kill",
            "mode": "one",
            "selector": {"namespaces": ["{target_namespace}"]},
            "gracePeriod": 0,
            "duration": "{duration}",
        },
    },
    # Pod-failure: makes a pod unavailable (injected pause) → CrashLoopBackOff
    {
        "name": "pod-failure",
        "kind": "PodChaos",
        "spec": {
            "action": "pod-failure",
            "mode": "one",
            "selector": {"namespaces": ["{target_namespace}"]},
            "duration": "{duration}",
        },
    },
    # ── Network faults ────────────────────────────────────────────────
    # High latency: 500ms base + jitter → visible in trace spans
    {
        "name": "network-delay",
        "kind": "NetworkChaos",
        "spec": {
            "action": "delay",
            "mode": "one",
            "selector": {"namespaces": ["{target_namespace}"]},
            "delay": {
                "latency": "500ms",
                "correlation": "50",
                "jitter": "200ms",
            },
            "duration": "{duration}",
        },
    },
    # Packet loss: 50% → visible as intermittent request failures
    {
        "name": "network-loss",
        "kind": "NetworkChaos",
        "spec": {
            "action": "loss",
            "mode": "one",
            "selector": {"namespaces": ["{target_namespace}"]},
            "loss": {"loss": "50", "correlation": "50"},
            "duration": "{duration}",
        },
    },
]
