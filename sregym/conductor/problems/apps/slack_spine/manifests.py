"""Slack Spine: manifests."""

from __future__ import annotations

import copy
import json
import re
import time

APP = "slack_spine"


_NOISY_METADATA = ("creationTimestamp", "generation", "managedFields", "resourceVersion", "selfLink", "uid")


def _clean_manifest(obj: dict) -> dict:
    """A live object as a manifest that can be re-created after deletion."""
    manifest = copy.deepcopy(obj)
    manifest.pop("status", None)
    meta = manifest.setdefault("metadata", {})
    for field in _NOISY_METADATA:
        meta.pop(field, None)
    annotations = meta.get("annotations") or {}
    annotations.pop("kubectl.kubernetes.io/last-applied-configuration", None)
    annotations.pop("deployment.kubernetes.io/revision", None)
    if annotations:
        meta["annotations"] = annotations
    else:
        meta.pop("annotations", None)
    if manifest.get("kind") == "Service":
        spec = manifest.get("spec", {})
        for field in ("clusterIP", "clusterIPs"):
            spec.pop(field, None)
    return manifest


def _get(problem, kind: str, name: str) -> dict:
    return json.loads(problem.kubectl.exec_command_checked(f"kubectl get {kind} {name} -n {problem.namespace} -o json"))


def _apply(problem, manifest: dict) -> None:
    problem.kubectl.exec_command_checked(f"kubectl apply -n {problem.namespace} -f -", input_data=json.dumps(manifest))


def _rollout_status(problem, deployment: str, timeout_s: int = 300) -> None:
    problem.kubectl.exec_command(
        f"kubectl rollout status deployment/{deployment} -n {problem.namespace} --timeout={timeout_s}s"
    )


def _replace_pods(problem, deployment: str) -> None:
    """Scale 0 -> 1 so the only pod runs the current (faulty) template.

    A rolling update of a one-replica Deployment keeps the old pod serving
    until the new one is Ready; a fault whose pod never becomes Ready would
    otherwise stay invisible behind it.
    """
    selector = ",".join(
        f"{k}={v}"
        for k, v in problem.kubectl.get_deployment(deployment, problem.namespace).spec.selector.match_labels.items()
    )
    problem.kubectl.exec_command_checked(f"kubectl scale deployment {deployment} -n {problem.namespace} --replicas=0")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        pods = problem.kubectl.core_v1_api.list_namespaced_pod(problem.namespace, label_selector=selector).items
        if not pods:
            break
        time.sleep(3)
    problem.kubectl.exec_command_checked(f"kubectl scale deployment {deployment} -n {problem.namespace} --replicas=1")


_URL_PORT = re.compile(r"^(?P<scheme>[a-z][a-z0-9+.-]*://)?(?P<host>[^:/]+):(?P<port>\d+)(?P<rest>.*)$")


def _with_port(value: str, port: str) -> str:
    match = _URL_PORT.match(value)
    if not match:
        raise ValueError(f"Cannot find a host:port in {value!r}")
    return f"{match['scheme'] or ''}{match['host']}:{port}{match['rest']}"


def _selector(problem, deployment: str) -> str:
    """``k=v,...`` label selector of a Deployment's own pods (from ``spec.selector.matchLabels``)."""
    dep = problem.kubectl.get_deployment(deployment, problem.namespace)
    return ",".join(f"{k}={v}" for k, v in sorted(dep.spec.selector.match_labels.items()))


def _clean_deployment_manifest(deployment_yaml: dict) -> dict:
    """A Deployment manifest from ``kubectl get -o yaml`` that can be re-created after a delete."""
    dyaml = copy.deepcopy(deployment_yaml)
    meta = dyaml.get("metadata", {})
    for key in ("resourceVersion", "uid", "creationTimestamp", "generation", "managedFields"):
        meta.pop(key, None)
    (meta.get("annotations") or {}).pop("deployment.kubernetes.io/revision", None)
    dyaml.pop("status", None)
    return dyaml


_SURGE_CLIENT = r"""
import os, threading, urllib.request
target = os.environ["SURGE_TARGET"]
def loop(i):
    n = 0
    while True:
        n += 1
        url = "%s/channels/chan-%d/messages?limit=50" % (target, (i + n) % 16)
        try:
            urllib.request.urlopen(url, timeout=5).read()
        except Exception:
            pass
for i in range(int(os.environ.get("SURGE_THREADS", "32"))):
    threading.Thread(target=loop, args=(i,), daemon=True).start()
threading.Event().wait()
"""
