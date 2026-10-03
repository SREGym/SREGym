"""SREGym-Lite's Kafka poison-pill head-of-line block, re-targeted at Slack Spine.

The original builds a private order pipeline on Astronomy Shop's Kafka whose
consumer stalls forever on one malformed record. Slack Spine already has that
shape in its own async tier: ``svc-message`` enqueues every sent message
(through ``kafkagate``) onto the ``jobs.index`` Redpanda topic, keyed by
channel, and the ``worker-index`` lane (consumer group ``index``) indexes each
record into the search engine, committing offsets only after handling. A
record the worker rejects as ``document_syntax`` (a ``schema_version: v2`` +
``body_encoding: legacy_blocks`` message from a legacy client) is retried at
the head of its partition, with the partition paused, until the lane's error
policy sends it to ``jobs.index.dlq``.

The port sets an error policy that never quarantines ``document_syntax``
failures (``ERROR_POLICY_JSON`` on ``worker-index``) and sends one such
message through the real send path. Its partition stops: messages in every
channel hashed to that partition are stored and delivered but never become
searchable, while the pods stay Running and Ready and a restart re-reads the
same uncommitted record.

The user-facing symptom is search freshness, not HTTP errors: sends still
return 201 and the load generator's ``session_search`` only checks that
``/search`` answers with a well-formed hit list, so its error rate is
unaffected. The app-health half of the oracle therefore guards the fix (the
agent must not break the app while repairing the lane); the fault itself is
measured by :class:`IndexLaneProgressOracle`, which drives probe messages
through the same send -> Redpanda -> worker -> search path.
"""

from __future__ import annotations

import json
import shlex
import time
import uuid

from sregym.conductor.oracles.data_plane_progress import DataPlaneProgressOracle, PipelineValidationError
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.lite_ia.k8s import ported
from sregym.utils.decorators import mark_fault_injected

# Runs in the ops toolbox (stdlib only): post messages through svc-message,
# then look them up through svc-search. ``must`` ids are re-queried for up to
# ``wait_s`` seconds because the engine publishes new documents asynchronously.
_TOOLBOX_PROBE = r"""
import json, sys, time, urllib.parse, urllib.request

spec = json.loads(sys.argv[1])


def call(url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="GET" if body is None else "POST",
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read().decode()


posted = []
for message in spec.get("post", []):
    status, body = call(spec["message_url"] + "/messages", message)
    if not 200 <= status < 300:
        raise SystemExit("send %s returned %s: %s" % (message["client_msg_id"], status, body[:200]))
    posted.append(message["client_msg_id"])


def found(doc_id):
    query = urllib.parse.urlencode({"q": doc_id, "org_id": spec["org_id"]})
    status, body = call(spec["search_url"] + "/search?" + query)
    if not 200 <= status < 300:
        raise SystemExit("search returned %s: %s" % (status, body[:200]))
    return any(isinstance(h, dict) and h.get("id") == doc_id for h in json.loads(body).get("hits", []))


hits = {doc_id for doc_id in spec.get("search", []) if found(doc_id)}
must = [doc_id for doc_id in spec.get("must", []) if doc_id not in hits]
deadline = time.time() + float(spec.get("wait_s", 0))
while must and time.time() < deadline:
    time.sleep(1)
    hits.update(doc_id for doc_id in must if found(doc_id))
    must = [doc_id for doc_id in must if doc_id not in hits]
print("PROBE=" + json.dumps({"posted": posted, "found": sorted(hits)}))
"""


class IndexLaneProgressOracle(DataPlaneProgressOracle):
    """``DataPlaneProgressOracle`` over Slack Spine's ``jobs.index`` lane.

    Keeps the original evaluation (progress past the blocked offset, forward
    progress over a window, progress again after the consumer pod is deleted)
    and replaces only the snapshot. The source is the poisoned partition of
    ``jobs.index``; a record counts as processed when the search engine returns
    it. Each snapshot first sends a fresh probe message into the poisoned
    channel, playing the original's ``order-stream`` producer, so progress can
    be measured. Integrity: the poison record must still be in the log and never
    become searchable, and every probe below the group's committed offset must
    be searchable or quarantined in ``jobs.index.dlq`` (nothing valid skipped).
    """

    def _pipeline_snapshot(self) -> tuple[dict[int, str], int | None]:
        problem = self.problem
        problem.send_probes(1)
        time.sleep(3)
        committed = problem.committed_offset()
        records = problem.partition_records(problem.history_start)

        poison = records.get(problem.poison_offset)
        if poison is None or poison.get("id") != problem.poison_id:
            raise PipelineValidationError("the original invalid source record is no longer present")
        probes = {offset: value["id"] for offset, value in records.items() if problem.is_probe(value)}
        history = [offset for offset in probes if offset < problem.poison_offset]
        if len(history) < problem.HISTORY_COUNT:
            raise PipelineValidationError("original valid message history is no longer in the source partition")

        handled = [offset for offset in probes if committed is not None and offset < committed]
        found = problem.search(
            [*probes.values(), problem.poison_id], must=[probes[offset] for offset in handled], wait_s=12
        )
        if problem.poison_id in found:
            raise PipelineValidationError(f"invalid source offset {problem.poison_offset} was indexed as valid")
        missing = [offset for offset in handled if probes[offset] not in found]
        if missing:
            quarantined = problem.dlq_offsets()
            missing = [offset for offset in missing if offset not in quarantined]
        if missing:
            raise PipelineValidationError(
                f"valid source records were skipped, including offsets {sorted(missing)[:10]}"
            )

        processed = {offset: doc_id for offset, doc_id in probes.items() if doc_id in found}
        if any(offset not in processed for offset in history):
            raise PipelineValidationError("original valid message history was not fully processed")
        print(
            f"   partition {problem.TOPIC}[{problem.poison_partition}]: committed={committed} "
            f"probes={len(probes)} searchable={len(processed)} high={max(processed, default=None)}"
        )
        return processed, committed


class KafkaPoisonPillHOLBlockIA(Problem):
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
        ported(
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
    def _broker_pod(self) -> str:
        pods = self.app.pod_names(self.BROKER_SELECTOR)
        if not pods:
            raise RuntimeError("no running Redpanda broker pod found")
        return pods[0]

    def rpk(self, command: str, timeout: float = 60) -> str:
        return self.app.exec_in(f"pod/{self._broker_pod()}", f"rpk {command}", container="redpanda", timeout=timeout)

    def _toolbox_probe(self, spec: dict) -> dict:
        spec = {"message_url": "http://svc-message:8000", "search_url": "http://svc-search:8000", **spec}
        out = self.app.toolbox_exec(f"python3 - {shlex.quote(json.dumps(spec))}", input_data=_TOOLBOX_PROBE, timeout=90)
        for line in out.splitlines():
            if line.startswith("PROBE="):
                return json.loads(line.removeprefix("PROBE="))
        raise RuntimeError(f"toolbox probe returned no result: {out[-500:]!r}")

    def is_probe(self, value: dict) -> bool:
        return str(value.get("id", "")).startswith(self.PROBE_PREFIX + self.run_tag)

    def _message(self, doc_id: str, text: str, **extra) -> dict:
        return {"channel_id": self.channel_id, "client_msg_id": doc_id, "text": f"{text} {doc_id}", **extra}

    def send_probes(self, count: int) -> list[str]:
        ids = [f"{self.PROBE_PREFIX}{self.run_tag}{uuid.uuid4().hex[:12]}" for _ in range(count)]
        self._toolbox_probe({"post": [self._message(i, "search freshness check") for i in ids], "org_id": self.org_id})
        return ids

    def search(self, ids: list[str], must: list[str] = (), wait_s: float = 0) -> set[str]:
        result = self._toolbox_probe({"search": ids, "must": list(must), "wait_s": wait_s, "org_id": self.org_id})
        return set(result["found"])

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

    def _rollout(self) -> None:
        self.kubectl.exec_command_checked(
            f"kubectl rollout status deployment/{self.CONSUMER_DEPLOYMENT} -n {self.namespace} --timeout=300s",
            timeout=330,
        )

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


__all__ = ["IndexLaneProgressOracle", "KafkaPoisonPillHOLBlockIA"]
