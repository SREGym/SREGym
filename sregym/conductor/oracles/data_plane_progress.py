"""Validate durable progress through a Kafka-backed data plane."""

import json
import time

from kubernetes import client

from sregym.conductor.oracles.base import Oracle
from sregym.generators.fault.inject_kafka import KafkaBrokerClient


class DataPlaneProgressOracle(Oracle):
    importance = 1.0

    def __init__(
        self,
        problem,
        consumer_group: str,
        topic: str,
        output_topic: str,
        consumer_deployment: str,
        settle_seconds: int = 30,
        progress_timeout: int = 240,
        progress_window_seconds: int = 45,
        restart_recover_timeout: int = 300,
    ):
        super().__init__(problem)
        self.consumer_group = consumer_group
        self.topic = topic
        self.output_topic = output_topic
        self.consumer_deployment = consumer_deployment
        self.settle_seconds = settle_seconds
        self.progress_timeout = progress_timeout
        self.progress_window_seconds = progress_window_seconds
        self.restart_recover_timeout = restart_recover_timeout
        self.broker = KafkaBrokerClient(problem.kubectl, problem.namespace)

    @staticmethod
    def _is_ready(pod) -> bool:
        statuses = pod.status.container_statuses or []
        return pod.status.phase == "Running" and bool(statuses) and all(status.ready for status in statuses)

    def _deployment_pods(self):
        deployment = self.problem.kubectl.apps_v1_api.read_namespaced_deployment(
            self.consumer_deployment, self.problem.namespace
        )
        labels = deployment.spec.selector.match_labels or {}
        pods = []
        for pod in self.problem.kubectl.list_pods(self.problem.namespace).items:
            pod_labels = pod.metadata.labels or {}
            if not all(pod_labels.get(key) == value for key, value in labels.items()):
                continue
            owners = pod.metadata.owner_references or []
            if any(owner.kind == "ReplicaSet" for owner in owners):
                pods.append(pod)
        return pods

    def _ready_consumer_pods(self):
        return [
            pod for pod in self._deployment_pods() if pod.metadata.deletion_timestamp is None and self._is_ready(pod)
        ]

    def _pipeline_snapshot(self) -> tuple[dict[int, str], int | None]:
        state = self.broker.pipeline_snapshot(self.topic, self.output_topic, self.consumer_group)
        source_records = state["source"]
        output_records = state["output"]

        valid_source: dict[int, str] = {}
        invalid_source: set[int] = set()
        for record in source_records:
            offset = record["offset"]
            value = record["value"]
            try:
                record = json.loads(value)
                order_id = record["order_id"]
                if not isinstance(order_id, str):
                    raise ValueError("order_id is not a string")
                valid_source[offset] = order_id
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                invalid_source.add(offset)

        blocked_offset = self.problem.poison_offset
        if blocked_offset not in invalid_source:
            raise ValueError("the original invalid source record is no longer present")

        processed: dict[int, str] = {}
        for record in output_records:
            value = record["value"]
            try:
                result = json.loads(value)
                source_offset = int(result["source_offset"])
                order_id = result["order_id"]
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError("processed topic contains an invalid result") from exc
            if source_offset in invalid_source:
                raise ValueError(f"invalid source offset {source_offset} was treated as a valid order")
            expected = valid_source.get(source_offset)
            if expected is None or expected != order_id:
                raise ValueError(f"processed result does not match source offset {source_offset}")
            previous = processed.setdefault(source_offset, order_id)
            if previous != order_id:
                raise ValueError(f"conflicting processed results for source offset {source_offset}")

        high = max(processed, default=-1)
        missing = sorted(offset for offset in valid_source if offset <= high and offset not in processed)
        if missing:
            raise ValueError(f"valid source records were skipped, including offsets {missing[:10]}")

        expected_initial_ids = {f"ORD-{100000 + index}" for index in range(self.problem.poison_offset)}
        processed_ids = set(processed.values())
        if not expected_initial_ids.issubset(processed_ids):
            raise ValueError("original valid order history was not fully processed")

        return processed, state["group_offset"]

    def _await_progress_past(self, offset: int, timeout: int):
        deadline = time.time() + timeout
        last_error = None
        while time.time() < deadline:
            try:
                processed, group_offset = self._pipeline_snapshot()
                if processed and max(processed) > offset and group_offset is not None and group_offset > offset:
                    return processed, group_offset
            except (RuntimeError, ValueError) as exc:
                last_error = str(exc)
            time.sleep(10)
        return None, last_error

    def evaluate(self) -> dict:
        print("== Data-Plane Progress Oracle ==")
        blocked_offset = getattr(self.problem, "poison_offset", None)
        if blocked_offset is None:
            return {"success": False, "reason": "blocked source offset is unknown"}

        print(f"⏳ Settling {self.settle_seconds}s before evaluation...")
        time.sleep(self.settle_seconds)
        print("   Comparing source records, processed results, and the consumer-group offset...")
        progress, detail = self._await_progress_past(blocked_offset, self.progress_timeout)
        if progress is None:
            print("❌ The Kafka data plane did not make valid progress beyond the blocked record")
            return {"success": False, "reason": detail or "consumer group did not advance"}
        high = max(progress)
        print(f"   valid source results are complete through offset {high}; group offset={detail}")

        time.sleep(self.progress_window_seconds)
        try:
            progress_after, _ = self._pipeline_snapshot()
        except (RuntimeError, ValueError) as exc:
            return {"success": False, "reason": str(exc)}
        high_after = max(progress_after, default=high)
        print(f"   forward progress: {high} -> {high_after}")
        if high_after <= high:
            print("❌ No new valid records were processed")
            return {"success": False, "reason": "no forward progress"}

        running = self._ready_consumer_pods()
        if not running:
            return {"success": False, "reason": "consumer Deployment has no Ready pod before restart probe"}
        victim = running[0]
        victim_name = victim.metadata.name
        victim_uid = victim.metadata.uid
        print(f"🔁 Restart-resistance probe: deleting consumer pod {victim_name}")
        self.problem.kubectl.core_v1_api.delete_namespaced_pod(
            victim_name,
            self.problem.namespace,
            body=client.V1DeleteOptions(grace_period_seconds=0),
        )

        baseline = high_after
        deadline = time.time() + self.restart_recover_timeout
        last_error = None
        while time.time() < deadline:
            replacement = any(pod.metadata.uid != victim_uid for pod in self._ready_consumer_pods())
            if replacement:
                try:
                    now, _ = self._pipeline_snapshot()
                    now_high = max(now, default=baseline)
                    if now_high > baseline:
                        print(f"   post-restart progress: {baseline} -> {now_high}")
                        print("✅ Kafka source/output integrity and durable consumer progress verified")
                        return {"success": True}
                except (RuntimeError, ValueError) as exc:
                    last_error = str(exc)
            time.sleep(10)
        print("❌ Pipeline did not resume valid processing after consumer replacement")
        return {"success": False, "reason": last_error or "not restart-resistant"}
