"""Fetches astronomy-shop product images the way the frontend's browser code does.

The demo applies imageSlowLoad only to requests carrying x-envoy-fault-delay-request,
which the frontend's browser JavaScript sets from the flag. Locust's HTTP users never
fetch images, and the demo 2.2.0 browser users crash, so nothing else sends it.

The client runs in a namespace hidden from the agent (agent_visibility_policy.HIDDEN_NAMESPACES).
Its command names the flag, and deleting it would clear the latency alert without a fix.
"""

import json
import math
import time
from pathlib import Path

from kubernetes import client

IMAGE_CLIENT_NAME = "frontend-image-client"
IMAGE_CLIENT_NAMESPACE = "khaos"
IMAGE_CLIENT_IMAGE = "python:3.12-slim"
FLAG = "imageSlowLoad"
INTERVAL_SECONDS = 5
HEALTHY_LATENCY_S = 2.0
INJECTED_MIN_LATENCY_S = 8.0
_MAX_BATCH_AGE_S = 60


class ImageEvidenceError(RuntimeError):
    def __init__(self, reason: str, detail: str):
        self.reason = reason
        super().__init__(detail)


def _fresh_batches(logs, after, now):
    batches = {}
    for line in logs.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        started_at = row.get("started_at")
        if type(started_at) not in (float, int) or not max(after, now - _MAX_BATCH_AGE_S) <= started_at <= now:
            continue
        durations = row.get("durations_s")
        if (
            isinstance(durations, list)
            and durations
            and all(
                type(duration) in (int, float) and math.isfinite(duration) and duration >= 0 for duration in durations
            )
        ):
            batches[started_at] = {"durations_s": durations}
        else:
            batches[started_at] = {"error": str(row.get("error", "invalid image sample"))}
    return [batches[stamp] for stamp in sorted(batches)]


def wait_for_images(kubectl, *, after, timeout, delayed=False, batches_required=2, alert_is_firing=None):
    """Require consecutive recent batches. Silence and failed requests do not count."""
    deadline = time.monotonic() + timeout
    while True:
        reason = "image_evidence_missing"
        detail = "no fresh successful image requests from the workload"
        try:
            logs = kubectl.exec_command_checked(
                f"kubectl logs deployment/{IMAGE_CLIENT_NAME} -n {IMAGE_CLIENT_NAMESPACE} --tail=30", timeout=8
            )
            batches = _fresh_batches(logs, after, time.time())[-batches_required:]
            if batches and "error" in batches[-1]:
                reason, detail = "image_request_failed", batches[-1]["error"]
            durations = [batch["durations_s"] for batch in batches if "durations_s" in batch]
            if len(durations) == batches_required:
                fastest = min(min(batch) for batch in durations)
                slowest = max(max(batch) for batch in durations)
                detail = f"{sum(map(len, durations))} image responses, latency {fastest:.3f}–{slowest:.3f}s"
                matches = fastest >= INJECTED_MIN_LATENCY_S if delayed else slowest <= HEALTHY_LATENCY_S
                if matches and (alert_is_firing is None or alert_is_firing()):
                    return detail
                if matches:
                    reason = "image_latency_alert_missing"
                elif not delayed and fastest >= INJECTED_MIN_LATENCY_S:
                    reason = "fault_still_present"
                else:
                    reason = "image_latency_outside_target"
        except RuntimeError as exc:
            reason, detail = "image_measurement_unavailable", str(exc)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ImageEvidenceError(reason, detail)
        time.sleep(min(3, remaining))


def image_client_deployment(app_namespace: str) -> client.V1Deployment:
    labels = {"app": IMAGE_CLIENT_NAME}
    container = client.V1Container(
        name="client",
        image=IMAGE_CLIENT_IMAGE,
        command=[
            "python",
            "-u",
            "-c",
            Path(__file__).with_name("image_client_worker.py").read_text(),
            app_namespace,
            FLAG,
            str(INTERVAL_SECONDS),
        ],
        # No CPU limit: a 100m limit throttled the parallel fetches.
        resources=client.V1ResourceRequirements(requests={"cpu": "10m", "memory": "16Mi"}, limits={"memory": "64Mi"}),
    )
    return client.V1Deployment(
        metadata=client.V1ObjectMeta(name=IMAGE_CLIENT_NAME, namespace=IMAGE_CLIENT_NAMESPACE, labels=labels),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels=labels),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels),
                # The client holds no writable app state and can stop immediately.
                spec=client.V1PodSpec(containers=[container], termination_grace_period_seconds=0),
            ),
        ),
    )


def start_image_client(app_namespace: str) -> None:
    try:
        client.CoreV1Api().create_namespace(
            body=client.V1Namespace(metadata=client.V1ObjectMeta(name=IMAGE_CLIENT_NAMESPACE))
        )
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise
    apps = client.AppsV1Api()
    body = image_client_deployment(app_namespace)
    try:
        apps.create_namespaced_deployment(namespace=IMAGE_CLIENT_NAMESPACE, body=body)
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise
        apps.replace_namespaced_deployment(name=IMAGE_CLIENT_NAME, namespace=IMAGE_CLIENT_NAMESPACE, body=body)


def stop_image_client(kubectl) -> None:
    # Wait for old pods to leave before another problem starts using the app.
    kubectl.exec_command_checked(
        f"kubectl delete deployment/{IMAGE_CLIENT_NAME} -n {IMAGE_CLIENT_NAMESPACE}"
        " --ignore-not-found --cascade=foreground --wait=true --timeout=30s",
        timeout=35,
    )
