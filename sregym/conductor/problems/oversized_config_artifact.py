"""
Problem: a regenerated config artifact outgrows the consumer's compiled capacity.

This problem models a configuration-pipeline failure on the Hotel Reservation app.
A worker consumes a generated artifact (a feature/rule list) on startup and
preallocates for a fixed number of entries. An upstream regeneration step doubles
the artifact past that compiled capacity, so the worker aborts while loading it and
enters CrashLoopBackOff. The artifact itself is well-formed; the defect is its size
relative to a limit baked into the consumer, not a syntax error in any single value.

The real-world archetype is the Cloudflare outage of 2025-11-18. A database
permissions change caused a query to emit duplicate rows, which doubled the Bot
Management feature file past the roughly 200-feature capacity the proxy had
preallocated. The proxy hit the limit, the load path panicked, and a large share of
the network returned 5xx. The artifact was valid; it was simply larger than the
consumer was built to accept, and restarting the consumer did not help because the
oversized artifact was still being served to it.
"""

import json
import time

import yaml

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.oversized_config_mitigation import OversizedConfigMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class OversizedConfigArtifact(Problem):
    """Deploy a config-driven worker, then feed it an artifact larger than its capacity."""

    CONSUMER_NAME = "traffic-classifier"
    CONFIG_NAME = "traffic-classifier-config"
    CONFIG_KEY = "features.list"
    CONFIG_MOUNT = "/etc/classifier"
    CONSUMER_IMAGE = "busybox:1.36"
    READY_MARKER = "/tmp/classifier-ready"

    # Capacity the consumer is "compiled" for. The oversized artifact exceeds it; the
    # known-good artifact stays within it.
    COMPILED_CAPACITY = 200
    OVERSIZED_FEATURE_COUNT = 400
    KNOWN_GOOD_FEATURE_COUNT = 60


    def __init__(self):
        self.app = HotelReservation()
        self.namespace = self.app.namespace

        super().__init__(app=self.app, namespace=self.namespace)

        self.kubectl = KubeCtl()
        self.faulty_service = self.CONSUMER_NAME

        self.root_cause = self.build_structured_root_cause(
            component=f"Deployment/{self.CONSUMER_NAME} consuming ConfigMap/{self.CONFIG_NAME}",
            namespace=self.namespace,
            description=(
                f"`Deployment/{self.CONSUMER_NAME}` loads a generated artifact (`{self.CONFIG_KEY}` in "
                f"`ConfigMap/{self.CONFIG_NAME}`) at startup and accepts at most {self.COMPILED_CAPACITY} entries. "
                "The artifact was regenerated far larger than that, so the worker rejects it during load, exits "
                "non-zero, and enters CrashLoopBackOff. The artifact is well-formed, so this is not a malformed "
                "value or a missing key: the defect is the entry count exceeding the capacity the consumer was "
                "built for. The rest of the namespace and the node are healthy (no DiskPressure, no eviction, no "
                "OOM), which is why pod-level and node-level checks elsewhere look clean. A complete diagnosis "
                "should connect the worker's abort message to the oversized artifact rather than to a syntax error "
                "or a node resource problem. A valid mitigation brings the artifact back within the consumer's "
                "capacity (or raises that capacity) so the worker loads it and becomes Ready while the artifact "
                "still carries its entries. Restarting the worker without changing the artifact does not help, and "
                "deleting the worker or emptying the artifact removes the function instead of restoring it."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()

        self.mitigation_oracle = OversizedConfigMitigationOracle(
            problem=self,
            deployment_name=self.CONSUMER_NAME,
            config_name=self.CONFIG_NAME,
            config_key=self.CONFIG_KEY,
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self._apply_config(self._render_features(self.OVERSIZED_FEATURE_COUNT))
        self._apply_consumer()
        self._wait_for_crashloop()
        print(f"Service: {self.CONSUMER_NAME} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.kubectl.exec_command(
            f"kubectl delete deployment {self.CONSUMER_NAME} -n {self.namespace} --ignore-not-found"
        )
        self.kubectl.exec_command(
            f"kubectl delete configmap {self.CONFIG_NAME} -n {self.namespace} --ignore-not-found"
        )
        self._wait_for_consumer_absent()
        print(f"Service: {self.CONSUMER_NAME} | Namespace: {self.namespace}\n")

    def _render_features(self, count: int) -> str:
        # One entry per line. The consumer counts non-empty lines, so the content is
        # what matters, not any particular value.
        return "".join(f"feature_{index}\n" for index in range(1, count + 1))

    def _apply_config(self, features: str):
        config_map = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": self.CONFIG_NAME, "namespace": self.namespace},
            "data": {self.CONFIG_KEY: features},
        }
        path = f"/tmp/{self.CONFIG_NAME}.yaml"
        with open(path, "w") as file:
            yaml.safe_dump(config_map, file)

        apply_out = self.kubectl.exec_command(f"kubectl apply -f {path} -n {self.namespace}")
        entry_count = features.count("\n")
        print(f"Applied ConfigMap/{self.CONFIG_NAME} with {entry_count} entries: {apply_out.strip()}")


    def _consumer_script(self) -> str:
        # The capacity is baked into the worker's startup path, the way a real consumer
        # would preallocate for a fixed number of entries. Counting non-empty lines keeps
        # the check independent of the value format.
        return (
            f"CAP={self.COMPILED_CAPACITY}\n"
            f'f={self.CONFIG_MOUNT}/{self.CONFIG_KEY}\n'
            f"ready={self.READY_MARKER}\n"
            'rm -f "$ready"\n'
            'n=$(grep -c . "$f" 2>/dev/null)\n'
            ": ${n:=0}\n"
            'echo "loaded $n features against compiled capacity $CAP"\n'
            'if [ "$n" -gt "$CAP" ]; then\n'
            '  echo "feature set $n exceeds compiled capacity $CAP, aborting load"\n'
            "  exit 1\n"
            "fi\n"
            'touch "$ready"\n'
            "exec tail -f /dev/null\n"
        )

    def _apply_consumer(self):
        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": self.CONSUMER_NAME,
                "namespace": self.namespace,
                "labels": {"app": self.CONSUMER_NAME},
            },
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": self.CONSUMER_NAME}},
                "template": {
                    "metadata": {"labels": {"app": self.CONSUMER_NAME}},
                    "spec": {
                        "containers": [
                            {
                                "name": "classifier",
                                "image": self.CONSUMER_IMAGE,
                                "imagePullPolicy": "IfNotPresent",
                                "command": ["/bin/sh", "-c"],
                                "args": [self._consumer_script()],
                                "volumeMounts": [{"name": "config", "mountPath": self.CONFIG_MOUNT}],
                                "readinessProbe": {
                                    "exec": {"command": ["test", "-f", self.READY_MARKER]},
                                    "initialDelaySeconds": 2,
                                    "periodSeconds": 5,
                                },
                            }
                        ],
                        "volumes": [{"name": "config", "configMap": {"name": self.CONFIG_NAME}}],
                    },
                },
            },
        }
        path = f"/tmp/{self.CONSUMER_NAME}.yaml"
        with open(path, "w") as file:
            yaml.safe_dump(deployment, file)

        apply_out = self.kubectl.exec_command(f"kubectl apply -f {path} -n {self.namespace}")
        print(f"Deployed consumer Deployment/{self.CONSUMER_NAME}: {apply_out.strip()}")


    def _wait_for_crashloop(self, timeout: int = 150):
        """Block until the consumer is failing to load the artifact, so the fault is active."""
        deadline = time.monotonic() + timeout
        last_state = "consumer pod not observed yet"

        while time.monotonic() < deadline:
            raw = self.kubectl.exec_command(
                f"kubectl get pods -n {self.namespace} -l app={self.CONSUMER_NAME} -o json"
            ).strip()
            pods = json.loads(raw).get("items", []) if raw.startswith("{") else []

            for pod in pods:
                for status in pod.get("status", {}).get("containerStatuses", []):
                    waiting = (status.get("state", {}).get("waiting") or {}).get("reason")
                    terminated = status.get("lastState", {}).get("terminated") or {}
                    if waiting == "CrashLoopBackOff" or terminated.get("exitCode"):
                        print(f"Consumer Deployment/{self.CONSUMER_NAME} is failing to load the artifact")
                        return
                    last_state = waiting or "starting"

            time.sleep(5)

        raise RuntimeError(
            f"Deployment/{self.CONSUMER_NAME} did not enter CrashLoopBackOff within {timeout}s. "
            f"Last observed container state: {last_state}"
        )


    def _wait_for_consumer_absent(self, timeout: int = 90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            existing = self.kubectl.exec_command(
                f"kubectl get deployment {self.CONSUMER_NAME} -n {self.namespace} --ignore-not-found -o name"
            ).strip()
            if not existing:
                return
            time.sleep(3)

        print(f"Warning: Deployment/{self.CONSUMER_NAME} still present after {timeout}s; recovery may be incomplete.")

