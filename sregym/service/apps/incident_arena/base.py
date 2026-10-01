"""Shared plumbing for the applications ported from Incident Arena.

Incident Arena (https://github.com/abundant-ai/incident-arena) packages each
incident as a Harbor task: a Helm chart for a whole system under test plus a
Harbor-specific harness (agent foothold, egress proxy, in-pod grader). The
charts live in SREGym-applications (``<app>/chart``; each app's README lists
provenance and the SREGym edits). SREGym deploys them with a values overlay
(``values/<app>.yaml``) that keeps the system under test and its load
generator, but replaces the harness with SREGym's observability stack and an
operator toolbox.
"""

from __future__ import annotations

import copy
import json
import logging
import shlex
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

from sregym.generators.workload.incident_arena import IncidentArenaLoadgen
from sregym.paths import TARGET_MICROSERVICES
from sregym.service.apps.base import Application
from sregym.service.helm import Helm
from sregym.service.kubectl import KubeCtl

logger = logging.getLogger("all.application.incident_arena")

PACKAGE_DIR = Path(__file__).resolve().parent
VALUES_DIR = PACKAGE_DIR / "values"

# Long enough that the load generator's schedule never ends inside a run; the
# Incident Arena tasks used a one-hour agent window instead.
LOAD_WINDOW_S = 86400.0


def deep_merge(base: dict, overlay: dict) -> dict:
    """Return ``base`` updated recursively with ``overlay`` (lists replace)."""
    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def continuous_load_profile(name: str, profile: dict, drop_event_kinds: tuple[str, ...] = ("admin_event",)) -> str:
    """Render a ``loadgen.profilesYaml`` document that never runs out of load.

    Incident Arena profiles end the schedule at ``declare_deadline_s`` (one
    agent window) and some carry scheduled fault triggers. SREGym keeps the
    profile's traffic shape, loops it for a day, and drops the trigger events
    whose kinds are listed in ``drop_event_kinds`` because problems inject those
    faults themselves at a controlled time.
    """
    body = copy.deepcopy(profile)
    body["loop"] = True
    body["declare_deadline_s"] = LOAD_WINDOW_S
    body.pop("undeclared_evidence_min_s", None)
    if "events" in body:
        body["events"] = [ev for ev in body["events"] if ev.get("kind") not in drop_event_kinds]
    return yaml.safe_dump({"profiles": {name: body}}, sort_keys=False)


class IncidentArenaApplication(Application):
    """A Helm-deployed Incident Arena system under test plus its load generator."""

    #: App directory in SREGym-applications and file under ``values/`` (without .yaml).
    CHART_NAME: str = ""
    #: Upper bound for the whole system to become ready after ``helm install``.
    READY_TIMEOUT_S: int = 1200
    #: Pods matching this selector run the operator toolbox (repair CLIs).
    TOOLBOX_SELECTOR = "app.kubernetes.io/component=ops-toolbox"

    def __init__(self, config_file):
        super().__init__(config_file)
        self.load_app_json()
        self.kubectl = KubeCtl()
        self.deploy_overrides: dict[str, Any] = {}
        self.create_namespace()

    # ------------------------------------------------------------------ metadata
    def load_app_json(self):
        super().load_app_json()
        metadata = self.get_app_json()
        self.app_name = metadata["Name"]
        self.description = metadata["Desc"]
        self.base_description = self.description
        self.helm_configs = {
            "release_name": metadata["Helm Config"]["release_name"],
            "namespace": self.namespace,
            "chart_path": str(TARGET_MICROSERVICES / self.CHART_NAME / "chart"),
        }

    @property
    def values_file(self) -> Path:
        return VALUES_DIR / f"{self.CHART_NAME}.yaml"

    # ------------------------------------------------------------------ deploy-time configuration
    def configure(self, values: dict) -> None:
        """Merge problem-specific chart values applied at ``helm install`` time."""
        self.deploy_overrides = deep_merge(self.deploy_overrides, values)

    def set_load_profile(self, name: str, profile: dict) -> None:
        """Run the load generator with ``profile`` (looped, without fault triggers)."""
        self.configure({"loadgen": {"profile": name, "profilesYaml": continuous_load_profile(name, profile)}})

    def _write_overrides(self) -> Path:
        with tempfile.NamedTemporaryFile(
            "w", prefix=f"sregym-{self.CHART_NAME}-", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            yaml.safe_dump(self.deploy_overrides, handle, sort_keys=False)
        return Path(handle.name)

    # ------------------------------------------------------------------ lifecycle
    def deploy(self):
        self.kubectl.create_namespace_if_not_exist(self.namespace)
        overrides = self._write_overrides()
        self.helm_configs["extra_args"] = ["-f", str(self.values_file), "-f", str(overrides)]
        Helm.install(**self.helm_configs)
        self.wait_until_ready()

    def wait_until_ready(self) -> None:
        deadline = time.monotonic() + self.READY_TIMEOUT_S
        self.kubectl.wait_for_ready(self.namespace, max_wait=self.READY_TIMEOUT_S)
        self.wait_for_jobs(max(60, int(deadline - time.monotonic())))

    def wait_for_jobs(self, timeout_s: int) -> None:
        """Wait for the chart's one-shot Jobs (migrate, seed, new-site) to complete.

        Pod readiness counts a running Job pod as ready, so without this the
        load generator can start against a system that is still being seeded.
        """
        jobs = self.kubectl.exec_command_checked(f"kubectl get jobs -n {self.namespace} -o name").split()
        if not jobs:
            return
        self.kubectl.exec_command_checked(
            f"kubectl wait --for=condition=complete {' '.join(jobs)} -n {self.namespace} --timeout={timeout_s}s",
            timeout=timeout_s + 30,
        )

    def create_workload(self):
        self.wrk = IncidentArenaLoadgen(self.namespace, self.kubectl)

    def start_workload(self):
        if not hasattr(self, "wrk"):
            self.create_workload()
        self.wrk.start()
        self.wrk.wait_for_traffic()

    def stop_workload(self):
        if hasattr(self, "wrk"):
            self.wrk.stop()

    def delete(self):
        Helm.uninstall(**self.helm_configs)
        self.kubectl.delete_namespace(self.namespace)
        self.kubectl.wait_for_namespace_deletion(self.namespace)

    def cleanup(self):
        Helm.uninstall(**self.helm_configs)
        self.kubectl.delete_namespace(self.namespace)

    # ------------------------------------------------------------------ helpers used by problems/oracles
    def pod_names(self, selector: str) -> list[str]:
        pods = self.kubectl.core_v1_api.list_namespaced_pod(self.namespace, label_selector=selector)
        return sorted(
            pod.metadata.name
            for pod in pods.items
            if pod.metadata.deletion_timestamp is None and pod.status.phase == "Running"
        )

    def pod_identities(self, selector: str) -> dict[str, dict[str, Any]]:
        """Pod UID, restart count and images per running pod (restart-masking basis)."""
        pods = self.kubectl.core_v1_api.list_namespaced_pod(self.namespace, label_selector=selector)
        identities = {}
        for pod in pods.items:
            if pod.metadata.deletion_timestamp is not None or pod.status.phase != "Running":
                continue
            statuses = pod.status.container_statuses or []
            identities[pod.metadata.name] = {
                "uid": pod.metadata.uid,
                "restarts": sum(int(s.restart_count or 0) for s in statuses),
                "images": sorted(c.image for c in pod.spec.containers),
            }
        return identities

    def exec_in(
        self,
        target: str,
        command: str,
        container: str | None = None,
        input_data: str | None = None,
        timeout: float = 120,
    ) -> str:
        """Run ``sh -c command`` inside ``target`` (``pod/x``, ``deploy/x``, ``sts/x``)."""
        container_flag = f" -c {container}" if container else ""
        stdin_flag = " -i" if input_data is not None else ""
        return self.kubectl.exec_command_checked(
            f"kubectl exec{stdin_flag} -n {self.namespace} {target}{container_flag} -- sh -c {shlex.quote(command)}",
            input_data=input_data,
            timeout=timeout,
        )

    def toolbox_exec(self, command: str, input_data: str | None = None, timeout: float = 120) -> str:
        """Run a shell command in the operator toolbox (has the DB clients and DSNs)."""
        return self.exec_in("deploy/ops-toolbox", command, container="toolbox", input_data=input_data, timeout=timeout)

    def http(
        self,
        url: str,
        method: str = "GET",
        body: Any = None,
        timeout: float = 30,
    ) -> tuple[int, str]:
        """Issue an in-cluster HTTP request from the toolbox; returns (status, body).

        Uses python3's urllib because every toolbox image ships python3 but not
        all of them ship curl.
        """
        request = {"url": url, "method": method, "body": body, "timeout": timeout}
        out = self.toolbox_exec(
            "python3 -c " + shlex.quote(_HTTP_CLIENT), input_data=json.dumps(request), timeout=timeout + 30
        )
        reply = json.loads(out.strip().splitlines()[-1])
        return int(reply["status"]), reply["body"]

    def rollout_restart(self, kind: str, name: str, timeout_s: int = 600) -> None:
        self.kubectl.exec_command_checked(f"kubectl rollout restart {kind}/{name} -n {self.namespace}", timeout=60)
        self.wait_rollout(kind, name, timeout_s)

    def wait_rollout(self, kind: str, name: str, timeout_s: int = 600) -> None:
        self.kubectl.exec_command_checked(
            f"kubectl rollout status {kind}/{name} -n {self.namespace} --timeout={timeout_s}s",
            timeout=timeout_s + 30,
        )

    def delete_pod_and_wait(self, kind: str, name: str, pod: str, timeout_s: int = 600) -> None:
        """Delete one pod and wait for its controller to bring a replacement up."""
        self.kubectl.exec_command_checked(f"kubectl delete pod {pod} -n {self.namespace} --wait=true", timeout=180)
        time.sleep(2)
        self.wait_rollout(kind, name, timeout_s)


# Reads {"url", "method", "body", "timeout"} on stdin, prints {"status", "body"}.
_HTTP_CLIENT = """
import json, sys, urllib.error, urllib.request
req = json.load(sys.stdin)
data = None if req["body"] is None else json.dumps(req["body"]).encode()
r = urllib.request.Request(req["url"], data=data, method=req["method"], headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(r, timeout=req["timeout"]) as resp:
        out = {"status": resp.status, "body": resp.read().decode("utf-8", "replace")}
except urllib.error.HTTPError as exc:
    out = {"status": exc.code, "body": exc.read().decode("utf-8", "replace")}
except Exception as exc:
    out = {"status": 0, "body": repr(exc)}
print(json.dumps(out))
"""
