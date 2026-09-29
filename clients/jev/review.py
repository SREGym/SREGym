"""Kubernetes snapshots and evidence checks for Jev submissions."""

import asyncio
import json
import re
import subprocess
from datetime import UTC, datetime

RESOURCES = "deployments,statefulsets,daemonsets,pods,services,endpointslices,persistentvolumeclaims,networkpolicies"


def review_guidance(result: dict, phase: str) -> dict:
    """Explain review scores without turning a triage ranking into approval."""
    if "error" in result:
        return {"assessment": "unavailable", "next_step": "Continue without this review; it supplied no advice."}
    required = ["causal_support"]
    if phase == "diagnose":
        required.append("active_failure")
    if phase == "verify":
        required.extend(["durable_repair", "functional_evidence"])
    scores = {key: result["answers"][key]["noul"] for key in required}
    weakest = min(scores.values())
    assessment = "unsupported" if weakest <= 0.5 else "uncertain" if weakest < 0.7 else "supported"
    return {
        "assessment": assessment,
        "claim_scores": scores,
        "next_step": (
            "The claim has support, but still requires your direct evidence and functional verification."
            if assessment == "supported"
            else "Do not describe this review as approval. Collect a new observation that distinguishes competing explanations before this repair or submission."
        ),
        "interpretation": "These are experimental guidance bands, not calibrated guarantees.",
    }


def compact_snapshot(document: dict) -> list[dict]:
    """Copy operational fields only; never copy env values, Secrets, or annotations."""
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        raise ValueError("Expected a Kubernetes resource list")
    selector_keys = {
        key
        for item in document["items"]
        if item.get("kind") == "Service"
        for key in (item.get("spec", {}).get("selector") or {})
    }
    snapshot = []
    for item in document.get("items", []):
        kind = item.get("kind")
        spec, status = item.get("spec", {}), item.get("status", {})
        row = {"kind": kind, "name": item.get("metadata", {}).get("name")}
        if kind in {"Deployment", "StatefulSet", "DaemonSet"}:
            pod = spec.get("template", {}).get("spec", {})
            row.update({k: spec[k] for k in ("replicas", "selector", "strategy", "updateStrategy") if k in spec})
            row["placement"] = {
                k: pod[k] for k in ("nodeSelector", "affinity", "topologySpreadConstraints") if k in pod
            }
            row["containers"] = [
                {k: container[k] for k in ("name", "image", "ports", "resources") if k in container}
                for container in pod.get("containers", [])
            ]
            row["status"] = {
                k: status[k]
                for k in (
                    "replicas",
                    "updatedReplicas",
                    "readyReplicas",
                    "availableReplicas",
                    "desiredNumberScheduled",
                    "numberReady",
                    "updatedNumberScheduled",
                )
                if k in status
            }
        elif kind == "Pod":
            row.update(
                node=spec.get("nodeName"),
                phase=status.get("phase"),
                labels={k: v for k, v in item.get("metadata", {}).get("labels", {}).items() if k in selector_keys},
            )
            row["containers"] = [
                {
                    "name": container.get("name"),
                    "ready": container.get("ready"),
                    "restarts": container.get("restartCount"),
                    "state": {
                        state: {k: value[k] for k in ("reason", "exitCode", "startedAt", "finishedAt") if k in value}
                        for state, value in container.get("state", {}).items()
                    },
                }
                for container in status.get("containerStatuses", [])
            ]
        elif kind == "Service":
            row["spec"] = {
                k: spec[k]
                for k in (
                    "type",
                    "selector",
                    "ports",
                    "clusterIP",
                    "externalName",
                    "internalTrafficPolicy",
                    "externalTrafficPolicy",
                    "sessionAffinity",
                    "publishNotReadyAddresses",
                    "trafficDistribution",
                )
                if k in spec
            }
        elif kind == "EndpointSlice":
            row["service"] = item.get("metadata", {}).get("labels", {}).get("kubernetes.io/service-name")
            row["ports"] = item.get("ports", [])
            row["endpoints"] = [
                {k: endpoint[k] for k in ("addresses", "conditions", "nodeName", "zone") if k in endpoint}
                for endpoint in (item.get("endpoints") or [])
            ]
        elif kind == "PersistentVolumeClaim":
            row["spec"] = {
                k: spec[k] for k in ("storageClassName", "accessModes", "resources", "volumeName") if k in spec
            }
            row["phase"] = status.get("phase")
        elif kind == "NetworkPolicy":
            row["spec"] = spec
        else:
            continue
        snapshot.append(row)
    return snapshot


async def collect_snapshot(namespace: str) -> dict:
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", namespace):
        raise ValueError("Use a valid Kubernetes namespace name")
    result = await asyncio.to_thread(
        subprocess.run,
        ["kubectl", "get", RESOURCES, "--namespace", namespace, "--request-timeout=10s", "-o", "json"],
        capture_output=True,
        text=True,
        timeout=12,
        check=False,
    )
    if result.returncode:
        raise ValueError("Cannot read the namespace snapshot through the configured Kubernetes connection")
    if len(result.stdout.encode()) > 4_194_304:
        raise ValueError("Namespace response is too large for this review")
    resources = compact_snapshot(json.loads(result.stdout))
    snapshot = {"observed_at": datetime.now(UTC).isoformat(), "namespace": namespace, "resources": resources}
    if len(json.dumps(snapshot).encode()) > 52_000:
        raise ValueError("Namespace snapshot exceeds the review evidence limit")
    return snapshot


def review_questions(phase: str) -> dict:
    questions = {
        "causal_support": {
            "type": "noul",
            "instructions": "Does the evidence establish the agent's proposed causal mechanism for an active or repeatable application failure? An unusual setting, historical error, or correlation alone is insufficient. If there is no proposed mechanism, answer no.",
        },
    }
    if phase == "diagnose":
        questions["active_failure"] = {
            "type": "noul",
            "instructions": "Do the supplied observations demonstrate a recent or repeatable failed application operation relevant to the proposed diagnosis? Historical startup errors, healthy-workload restart counts, unusual settings, and hypothetical risks alone are insufficient. Judge the actual evidence, not the agent's assertion that an outage exists.",
        }
    if phase == "verify":
        questions["causal_support"]["instructions"] = (
            "Do the supplied before-and-after observations support that the applied repair addressed the demonstrated cause of the original application failure? Evaluate the historical failure together with the current state. A healthy state after repair does not contradict a previously demonstrated failure, but fixing an unrelated anomaly or testing an unaffected path is insufficient."
        )
    if phase == "verify":
        questions["durable_repair"] = {
            "type": "noul",
            "instructions": "Does the proposed or applied repair correct the established mechanism while preserving application behavior during ordinary restarts, placement changes, and requests? A workaround that only avoids the currently failing path or relies on accidental placement is insufficient. Repeated successes under unchanged conditions alone do not prove this.",
        }
        questions["functional_evidence"] = {
            "type": "noul",
            "instructions": "Do the fresh functional tests exercise the actual failing behavior and support the claimed result? Healthy pods or tests of unaffected paths are insufficient. If no functional result is supplied, answer no.",
        }
    return questions
