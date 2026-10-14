"""Slack Spine: indexing."""

from __future__ import annotations

import json
import shlex
import time
import uuid

from sregym.conductor.oracles.apps.slack_spine.indexing import IndexLaneLagOracle, IndexLaneProgressOracle
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.environment import configure_port
from sregym.conductor.problems.kafka_queue_problems import KafkaQueueProblems
from sregym.service.apps.slack_spine.index_lane import IndexLaneOperations
from sregym.utils.decorators import mark_fault_injected


class KafkaQueueProblemsSlack(IndexLaneOperations, KafkaQueueProblems):
    """A queue overload meets a slowed consumer on Slack Spine's search-index lane.

    The original flips the OpenTelemetry demo's ``kafkaQueueProblems`` flag,
    which floods the Kafka topic and adds a consumer-side processing delay so
    consumer lag spikes. Here the ``worker-index`` lane (consumer group
    ``index`` on ``jobs.index``) gets its authored per-job cost raised
    (``HANDLER_MS``) and its concurrency dropped to one through its env, and a
    burst of messages is pushed through the real send path. The backlog keeps
    growing under the load generator's sends: messages are stored and delivered
    but become searchable only after many minutes. The load generator's search
    session only checks that ``/search`` answers, so the symptom is search
    freshness (and the lane's lag metric), not HTTP errors.
    """

    LANE = "index"
    TOPIC = "jobs.index"
    CONSUMER_GROUP = "index"
    CONSUMER_DEPLOYMENT = "worker-index"
    BROKER_SELECTOR = "app.kubernetes.io/component=redpanda"
    PROBE_PREFIX = "lagprobe"
    SLOW_ENV = {"HANDLER_MS": "4000", "LANE_CONCURRENCY": "1"}
    BURST = 300

    def __init__(self, app_name: str = "slack_spine", channel_id: str = "chan-0"):
        self.channel_id = channel_id
        self.org_id = f"org-{channel_id}"
        self.faulty_service = self.CONSUMER_DEPLOYMENT
        self.feature_flag = None
        self.run_tag = uuid.uuid4().hex[:8]
        self.backlog_ids: list[str] = []
        configure_port(
            self,
            app_name,
            component=f"deployment/{self.CONSUMER_DEPLOYMENT} (Redpanda topic {self.TOPIC}, consumer group {self.CONSUMER_GROUP})",
            description=(
                f"The `{self.TOPIC}` Redpanda queue is overloaded relative to its consumer. The "
                f"`{self.CONSUMER_DEPLOYMENT}` lane (consumer group `{self.CONSUMER_GROUP}`) had its per-job "
                f"processing cost raised to `HANDLER_MS={self.SLOW_ENV['HANDLER_MS']}` (default 8 ms) and its "
                f"parallelism cut to `LANE_CONCURRENCY={self.SLOW_ENV['LANE_CONCURRENCY']}` (default 4) through "
                "env vars on the deployment, so it drains well under one job per second, while a burst of "
                "message sends (plus the normal message traffic, enqueued by svc-message through kafkagate) "
                "filled the topic. Consumer lag (`kafka_consumergroup_lag`, `rpk group describe index`) keeps "
                "growing: new messages are stored and delivered but take many minutes to become searchable. Pods "
                "stay Running and Ready and no request fails. Mitigation: restore the lane's processing cost and "
                "concurrency (deployment env or the worker's `/admin/config`) so the backlog drains, without "
                "skipping (resetting offsets past) the queued messages."
            ),
            oracle_factory=IndexLaneLagOracle,
        )

    def total_lag(self) -> int:
        out = self.rpk(f"group describe {self.CONSUMER_GROUP}")
        for line in out.splitlines():
            if line.startswith("TOTAL-LAG"):
                return int(line.split()[1])
        raise RuntimeError(f"no TOTAL-LAG for group {self.CONSUMER_GROUP}: {out[-300:]!r}")

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection: slow index lane + queue burst ==")
        env = " ".join(f"{k}={v}" for k, v in self.SLOW_ENV.items())
        self.kubectl.exec_command_checked(
            f"kubectl set env deployment/{self.CONSUMER_DEPLOYMENT} -n {self.namespace} {env}"
        )
        self._rollout()
        ids = [f"{self.PROBE_PREFIX}{self.run_tag}{uuid.uuid4().hex[:12]}" for _ in range(self.BURST)]
        self.backlog_ids = []
        for start in range(0, len(ids), 100):
            chunk = ids[start : start + 100]
            accepted = self._toolbox_probe(
                {"post": [self._message(i, "bulk import") for i in chunk], "org_id": self.org_id}
            )
            self.backlog_ids.extend(accepted["posted"])
        if set(self.backlog_ids) != set(ids):
            raise RuntimeError("the message API did not accept the complete burst")
        time.sleep(20)
        print(f"Index lane slowed ({env}); {self.BURST} messages queued; lag now {self.total_lag()}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery: restore the index lane's cost and concurrency ==")
        env = " ".join(f"{k}-" for k in self.SLOW_ENV)
        self.kubectl.exec_command_checked(
            f"kubectl set env deployment/{self.CONSUMER_DEPLOYMENT} -n {self.namespace} {env}"
        )
        self._rollout()


class KafkaPoisonPillHOLBlockIA(IndexLaneOperations, Problem):
    """A legacy-format message blocks one partition of Slack Spine's search-index lane."""

    LANE = "index"
    TOPIC = "jobs.index"
    DLQ_TOPIC = "jobs.index.dlq"
    CONSUMER_GROUP = "index"
    CONSUMER_DEPLOYMENT = "worker-index"
    BROKER_SELECTOR = "app.kubernetes.io/component=redpanda"
    POLICY_ENV = "ERROR_POLICY_JSON"
    # Retry malformed documents "until they succeed": ~11 days at 1 attempt/s.
    STALL_POLICY = {"document_syntax": {"max_attempts": 1000000, "backoff_ms": 1000}}
    HISTORY_COUNT = 10
    FOLLOWER_COUNT = 5
    PROBE_PREFIX = "idxprobe"

    def __init__(self, app_name: str = "slack_spine", channel_id: str = "chan-0"):
        self.channel_id = channel_id
        self.org_id = f"org-{channel_id}"
        self.faulty_service = self.CONSUMER_DEPLOYMENT
        self.run_tag = uuid.uuid4().hex[:8]
        self.poison_id = f"legacyimport{self.run_tag}"
        self.poison_partition: int | None = None
        self.poison_offset: int | None = None
        self.history_start = 0
        configure_port(
            self,
            app_name,
            component=f"deployment/{self.CONSUMER_DEPLOYMENT} (Redpanda topic {self.TOPIC})",
            description=(
                f"A poison-pill record blocks one partition of the `{self.TOPIC}` Redpanda topic. A message "
                f"sent to channel `{channel_id}` from a legacy client (`schema_version: v2`, `body_encoding: "
                f"legacy_blocks`) was enqueued by svc-message/kafkagate onto `{self.TOPIC}`, and the "
                f"`{self.CONSUMER_DEPLOYMENT}` lane (consumer group `{self.CONSUMER_GROUP}`) rejects it as a "
                f"`document_syntax` error. The lane's error policy was overridden through the "
                f"`{self.POLICY_ENV}` env var on `{self.CONSUMER_DEPLOYMENT}` to retry `document_syntax` "
                "failures ~1,000,000 times instead of quarantining them to "
                f"`{self.DLQ_TOPIC}`, so the worker retries the record forever at the head of its partition, "
                "keeps that partition paused and never commits past it (head-of-line blocking). The group's "
                "committed offset on that partition is frozen and its lag grows; every later message in the "
                "channels hashed to that partition is stored and delivered but never indexed, so it never "
                "appears in search. The pods stay Running and Ready and other partitions keep flowing; "
                "restarting the worker re-reads the same uncommitted record and stalls again. Mitigation: get "
                "the poison record past the consumer without skipping the valid messages queued behind it, "
                f"e.g. restore a bounded `document_syntax` policy so it is quarantined to `{self.DLQ_TOPIC}` "
                "(worker `/admin/config` or the deployment env), or move the group's offset just past it."
            ),
            oracle_factory=lambda problem: IndexLaneProgressOracle(
                problem=problem,
                consumer_group=self.CONSUMER_GROUP,
                topic=self.TOPIC,
                output_topic=self.DLQ_TOPIC,
                consumer_deployment=self.CONSUMER_DEPLOYMENT,
                progress_timeout=150,
            ),
        )

    # ------------------------------------------------------------------ app helpers

    def is_probe(self, value: dict) -> bool:
        return str(value.get("id", "")).startswith(self.PROBE_PREFIX + self.run_tag)

    def committed_offset(self) -> int | None:
        out = self.rpk(f"group describe {self.CONSUMER_GROUP} -c")
        for line in out.splitlines():
            cols = line.split()
            if len(cols) >= 3 and cols[0] == self.TOPIC and cols[1] == str(self.poison_partition):
                return None if cols[2] == "-" else int(cols[2])
        raise RuntimeError(f"group {self.CONSUMER_GROUP} has no entry for {self.TOPIC}[{self.poison_partition}]")

    @staticmethod
    def _parse_records(out: str) -> list[tuple[int, int, dict]]:
        records = []
        for line in out.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            try:
                value = json.loads(parts[2])
            except json.JSONDecodeError:
                value = {"_raw": parts[2]}
            records.append((int(parts[0]), int(parts[1]), value if isinstance(value, dict) else {"_raw": value}))
        return records

    def partition_records(self, start: int) -> dict[int, dict]:
        out = self.rpk(f"topic consume {self.TOPIC} -p {self.poison_partition} -o {start}:end -f '%p\\t%o\\t%v\\n'")
        return {offset: value for _, offset, value in self._parse_records(out)}

    def dlq_offsets(self) -> set[int]:
        out = self.rpk(f"topic consume {self.DLQ_TOPIC} -p {self.poison_partition} -o :end -f '%k\\n'")
        prefix = f"{self.TOPIC}:{self.poison_partition}:"
        return {int(key.removeprefix(prefix)) for key in out.split() if key.startswith(prefix)}

    def _wait_searchable(self, ids: list[str], timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            missing = set(ids) - self.search(ids)
            if not missing:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{len(missing)} probe messages were never indexed before injection")
            time.sleep(5)

    # ------------------------------------------------------------------ fault
    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection: poison pill on Slack Spine's jobs.index lane ==")
        policy = json.dumps(self.STALL_POLICY, separators=(",", ":"))
        self.kubectl.exec_command_checked(
            f"kubectl set env deployment/{self.CONSUMER_DEPLOYMENT} -n {self.namespace} "
            f"{self.POLICY_ENV}={shlex.quote(policy)}"
        )
        self._rollout()

        # Valid history, then the poison, then valid messages queued behind it.
        history = self.send_probes(self.HISTORY_COUNT)
        self._wait_searchable(history, timeout_s=180)
        legacy = self._message(
            self.poison_id, "imported from legacy workspace", schema_version="v2", body_encoding="legacy_blocks"
        )
        self._toolbox_probe({"post": [legacy], "org_id": self.org_id})
        self.send_probes(self.FOLLOWER_COUNT)

        deadline = time.monotonic() + 60
        located = None
        while located is None:
            out = self.rpk(f"topic consume {self.TOPIC} -o :end -f '%p\\t%o\\t%v\\n'", timeout=120)
            records = self._parse_records(out)
            located = next(((p, o) for p, o, v in records if v.get("id") == self.poison_id), None)
            if located is None:
                if time.monotonic() >= deadline:
                    raise RuntimeError("the legacy-format message never reached jobs.index")
                time.sleep(3)
        self.poison_partition, self.poison_offset = located
        self.history_start = min(o for p, o, v in records if p == self.poison_partition and self.is_probe(v))

        # The lane must be stuck at the poison record, not quarantine it.
        time.sleep(20)
        committed = self.committed_offset()
        if committed != self.poison_offset:
            raise RuntimeError(
                f"{self.TOPIC}[{self.poison_partition}] committed offset {committed} did not stop at "
                f"the poison record (offset {self.poison_offset})"
            )
        print(
            f"Poison record at {self.TOPIC}[{self.poison_partition}] offset {self.poison_offset}; consumer group "
            f"'{self.CONSUMER_GROUP}' is stalled there."
        )

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery: restore the index lane's bounded error policy ==")
        self.kubectl.exec_command_checked(
            f"kubectl set env deployment/{self.CONSUMER_DEPLOYMENT} -n {self.namespace} {self.POLICY_ENV}-"
        )
        self._rollout()
