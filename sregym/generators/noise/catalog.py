"""
Pre-defined catalog of Chaos Mesh experiment templates.

Each template is a dict with:
  - name: human-readable name for logging
  - kind: Chaos Mesh CRD kind (PodChaos, NetworkChaos, StressChaos, IOChaos, etc.)
  - spec: the CRD spec with {target_namespace} and {duration} placeholders
"""

EXPERIMENT_CATALOG = [
    # ── Pod failures ──────────────────────────────────────────────────
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
    {
        "name": "container-kill",
        "kind": "PodChaos",
        "spec": {
            "action": "container-kill",
            "mode": "one",
            "selector": {"namespaces": ["{target_namespace}"]},
            "duration": "{duration}",
        },
    },
    # ── Network faults ────────────────────────────────────────────────
    {
        "name": "network-delay",
        "kind": "NetworkChaos",
        "spec": {
            "action": "delay",
            "mode": "one",
            "selector": {"namespaces": ["{target_namespace}"]},
            "delay": {
                "latency": "200ms",
                "correlation": "50",
                "jitter": "50ms",
            },
            "duration": "{duration}",
        },
    },
    {
        "name": "network-loss",
        "kind": "NetworkChaos",
        "spec": {
            "action": "loss",
            "mode": "one",
            "selector": {"namespaces": ["{target_namespace}"]},
            "loss": {"loss": "25", "correlation": "50"},
            "duration": "{duration}",
        },
    },
    {
        "name": "network-duplicate",
        "kind": "NetworkChaos",
        "spec": {
            "action": "duplicate",
            "mode": "one",
            "selector": {"namespaces": ["{target_namespace}"]},
            "duplicate": {"duplicate": "30", "correlation": "50"},
            "duration": "{duration}",
        },
    },
    # ── Stress ────────────────────────────────────────────────────────
    {
        "name": "cpu-stress",
        "kind": "StressChaos",
        "spec": {
            "mode": "one",
            "selector": {"namespaces": ["{target_namespace}"]},
            "stressors": {
                "cpu": {"workers": 1, "load": 50},
            },
            "duration": "{duration}",
        },
    },
    {
        "name": "memory-stress",
        "kind": "StressChaos",
        "spec": {
            "mode": "one",
            "selector": {"namespaces": ["{target_namespace}"]},
            "stressors": {
                "memory": {"workers": 1, "size": "128MB"},
            },
            "duration": "{duration}",
        },
    },
    # ── I/O faults ────────────────────────────────────────────────────
    {
        "name": "io-delay",
        "kind": "IOChaos",
        "spec": {
            "action": "latency",
            "mode": "one",
            "selector": {"namespaces": ["{target_namespace}"]},
            "delay": "100ms",
            "percent": 50,
            "volumePath": "/",
            "duration": "{duration}",
        },
    },
    {
        "name": "io-error",
        "kind": "IOChaos",
        "spec": {
            "action": "fault",
            "mode": "one",
            "selector": {"namespaces": ["{target_namespace}"]},
            "errno": 5,  # EIO
            "percent": 10,
            "volumePath": "/",
            "duration": "{duration}",
        },
    },
]
