"""Slack Spine: indexing."""

from __future__ import annotations

import time

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.data_plane_progress import DataPlaneProgressOracle, PipelineValidationError


class IndexLaneLagOracle(Oracle):
    """Slack Spine's search-index lane keeps up with its queue again.

    Passes when the ``index`` consumer group's total lag on ``jobs.index`` is
    back under ``max_lag``, all accepted burst messages are searchable, and a
    fresh message becomes searchable within ``freshness_s`` seconds.
    """

    FAILURE_CLASSES = {
        "lag_not_drained": "agent_error",
        "message_not_indexed": "agent_error",
        "accepted_messages_missing": "agent_error",
        "backlog_not_recorded": "ambiguous",
        "search_unavailable": "ambiguous",
    }

    def __init__(self, problem, max_lag: int = 25, freshness_s: float = 60):
        super().__init__(problem)
        self.max_lag = max_lag
        self.freshness_s = freshness_s

    def evaluate(self, *args, **kwargs) -> dict:
        print("== Index lane lag ==")
        problem = self.problem
        try:
            lag = problem.total_lag()
            probe = problem.send_probes(1)[0]
            found = probe in problem.search([probe], must=[probe], wait_s=self.freshness_s)
            lag_after = problem.total_lag()
        except Exception as exc:
            print(f"❌ Could not observe the index lane: {exc}")
            return self.fail("search_unavailable", error=str(exc))
        detail = {"lag": lag, "lag_after_probe": lag_after, "probe_searchable": found}
        if lag_after > self.max_lag:
            print(f"❌ {problem.CONSUMER_GROUP} lag on {problem.TOPIC} is {lag} -> {lag_after} (max {self.max_lag})")
            return self.fail("lag_not_drained", **detail)
        if not found:
            print(f"❌ probe message was not searchable within {self.freshness_s:.0f}s")
            return self.fail("message_not_indexed", **detail)
        if not problem.backlog_ids:
            return self.fail("backlog_not_recorded", **detail)
        try:
            indexed = problem.search(problem.backlog_ids, must=problem.backlog_ids, wait_s=self.freshness_s)
        except Exception as exc:
            return self.fail("search_unavailable", error=str(exc), **detail)
        missing = sorted(set(problem.backlog_ids) - indexed)
        detail.update(backlog_count=len(problem.backlog_ids), missing_count=len(missing))
        if missing:
            print(f"❌ {len(missing)} accepted messages are not searchable")
            return self.fail("accepted_messages_missing", missing_ids=missing, **detail)
        print(f"✅ lag {lag_after} <= {self.max_lag}; probe searchable within {self.freshness_s:.0f}s")
        return {"success": True, **detail}


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
