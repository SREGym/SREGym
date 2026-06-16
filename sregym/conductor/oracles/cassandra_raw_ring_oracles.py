"""Mitigation oracles for raw-ring Cassandra bug problems.

These problems reproduce real multi-node Cassandra bugs on a stock buggy image, so the
bug is in the binary and an agent cannot recompile Cassandra inside the run. The oracle's
job is therefore to *detect whether the documented buggy signature still manifests* on the
deployed cluster and report it verbatim:

  - ``success = False``  → the buggy signature is present (bug NOT mitigated);
  - ``success = True``   → the signature is gone (bug mitigated / running a fixed binary).

Two shapes are provided:

  * ``CassandraWrongResultOracle`` — re-establishes the per-replica divergence and runs a
    CQL query, classifying by returned row count (or a regex) against the buggy value.
  * ``CassandraLogGrepOracle`` — greps a pod's ``system.log`` / pod log / an arbitrary
    ``nodetool`` command output for the verbatim buggy line.
"""

import logging
import re

from sregym.conductor.oracles.base import Oracle

logger = logging.getLogger(__name__)


def _row_count(cqlsh_output: str) -> int | None:
    """Parse the trailing ``(N rows)`` cqlsh emits; None if not found."""
    m = re.search(r"\((\d+)\s+rows?\)", cqlsh_output)
    return int(m.group(1)) if m else None


class CassandraWrongResultOracle(Oracle):
    """Detect a wrong-result bug by (re)triggering it and inspecting the CQL output.

    Args:
        pod: coordinator pod to run the query on (default ``cass-0``).
        query: the discriminating CQL (e.g. a ``GROUP BY ... LIMIT`` short read).
        consistency: cqlsh CONSISTENCY level to prepend (default ``ALL``).
        buggy_regex: if set, the bug is present when this regex matches the output;
            otherwise the bug is present when the query returns >= ``min_buggy_rows`` rows.
        min_buggy_rows: row-count threshold for "bug present" when no regex is given.
        reestablish: when True, call ``problem.reestablish_divergence()`` before querying
            so the read is deterministic regardless of prior read-repair reconciliation.
    """

    importance = 1.0

    def __init__(
        self,
        problem,
        query: str,
        pod: str = "cass-0",
        consistency: str = "ALL",
        buggy_regex: str | None = None,
        min_buggy_rows: int = 1,
        reestablish: bool = True,
    ):
        super().__init__(problem)
        self.query = query
        self.pod = pod
        self.consistency = consistency
        self.buggy_regex = buggy_regex
        self.min_buggy_rows = min_buggy_rows
        self.reestablish = reestablish

    def evaluate(self) -> dict:
        app = self.problem.app
        if self.reestablish and hasattr(self.problem, "reestablish_divergence"):
            logger.info("[WrongResult] Re-establishing per-replica divergence before measuring")
            try:
                self.problem.reestablish_divergence()
            except Exception as e:
                logger.warning(f"[WrongResult] reestablish_divergence raised: {e}")

        cql = f"CONSISTENCY {self.consistency}; {self.query}"
        output = app.cqlsh(self.pod, cql)
        logger.info(f"[WrongResult] money query output:\n{output}")

        if self.buggy_regex is not None:
            present = bool(re.search(self.buggy_regex, output))
            detail = f"regex {self.buggy_regex!r} {'matched' if present else 'did not match'}"
        else:
            n = _row_count(output)
            present = n is not None and n >= self.min_buggy_rows
            detail = f"returned {n} row(s) (buggy when >= {self.min_buggy_rows})"

        signal = output.strip()
        reason = f"buggy wrong-result still present: {detail}" if present else f"wrong-result signature gone: {detail}"
        logger.info(f"[WrongResult] {'BUG PRESENT' if present else 'mitigated'} — {detail}")
        return {"success": not present, "bug_present": present, "reason": reason, "signal": signal}


class CassandraLogGrepOracle(Oracle):
    """Detect a bug whose signature is a server log / nodetool metric line.

    Args:
        pod: pod to read from (e.g. the surviving ``seed`` or the BOOT-parked ``joiner``).
        pattern: regex for the verbatim buggy line.
        source: ``system_log`` (``/var/log/cassandra/system.log``), ``pod_logs``
            (container stdout, optionally ``previous`` for a crashed container), or
            ``command`` (run ``command`` on the pod and grep its output).
        command: shell command for ``source='command'`` (e.g. ``nodetool cfstats ks.t``).
        previous: for ``source='pod_logs'``, read the previous (crashed) container's log.
        retrigger: when True, call ``problem.retrigger()`` before grepping.
    """

    importance = 1.0

    def __init__(
        self,
        problem,
        pod: str,
        pattern: str,
        source: str = "system_log",
        command: str | None = None,
        previous: bool = False,
        retrigger: bool = False,
        attempts: int = 1,
        retry_delay: float = 10.0,
    ):
        super().__init__(problem)
        self.pod = pod
        self.pattern = pattern
        self.source = source
        self.command = command
        self.previous = previous
        self.retrigger = retrigger
        self.attempts = attempts
        self.retry_delay = retry_delay

    def _read(self) -> str:
        app = self.problem.app
        if self.source == "command":
            return app.exec(self.pod, self.command or "true")
        if self.source == "pod_logs":
            # For a crash-looping pod read current + previous so the discriminating
            # startup line is caught regardless of which boot most recently logged it.
            if self.previous:
                return app.pod_logs(self.pod, previous=True)
            return app.pod_logs_all(self.pod)
        return app.system_log(self.pod)

    def evaluate(self) -> dict:
        import time

        if self.retrigger and hasattr(self.problem, "retrigger"):
            logger.info("[LogGrep] Re-triggering bug before grepping")
            try:
                self.problem.retrigger()
            except Exception as e:
                logger.warning(f"[LogGrep] retrigger raised: {e}")

        matches: list[str] = []
        text = ""
        for attempt in range(max(1, self.attempts)):
            text = self._read()
            matches = [ln for ln in text.splitlines() if re.search(self.pattern, ln)]
            if matches:
                break
            if attempt + 1 < self.attempts:
                logger.info(
                    f"[LogGrep] pattern not yet present (attempt {attempt + 1}/{self.attempts}); "
                    f"retrying in {self.retry_delay}s"
                )
                time.sleep(self.retry_delay)

        present = bool(matches)
        signal = matches[0].strip() if matches else ""
        if present:
            reason = f"buggy log signature present ({len(matches)} line(s)): {signal}"
        else:
            reason = f"buggy log signature {self.pattern!r} not found in {self.source}"
        logger.info(f"[LogGrep] {'BUG PRESENT' if present else 'absent'} — pattern={self.pattern!r}")
        return {"success": not present, "bug_present": present, "reason": reason, "signal": signal}
