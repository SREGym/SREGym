"""Helpers for stock-image single-node Cassandra bug reproductions."""

import logging
import re

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.problems.cassandra_raw_ring import CassandraRawRingProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class CassandraSingleNodeOracle(Oracle):
    """Run a problem's live observation and classify whether the buggy signature is present."""

    importance = 1.0

    def evaluate(self) -> dict:
        output = self.problem.last_observe_output()
        present = self.problem.bug_present(output)
        signal = self.problem.signal(output)
        reason = "buggy signature present" if present else "buggy signature absent"
        return {"success": not present, "bug_present": present, "reason": reason, "signal": signal}


class CassandraSingleNodeProblem(CassandraRawRingProblem):
    """Raw-ring problem specialization for one stock Cassandra pod."""

    replicas = 1
    pod = "cass-0"
    bug_pattern: str = ""
    bug_pattern_absent: bool = False

    def build_mitigation_oracle(self):
        return CassandraSingleNodeOracle(self)

    def sh(self, script: str, timeout: int = 180) -> str:
        return self.app.exec(self.pod, script, timeout=timeout)

    def cql(self, cql: str, timeout: int = 180, protocol_version: int | None = None) -> str:
        protocol = f" --protocol-version={protocol_version}" if protocol_version else ""
        return self.cql_file(cql, timeout=timeout, protocol_args=protocol)

    def cql_file(self, cql: str, timeout: int = 180, protocol_args: str = "") -> str:
        script = f"""cat >/tmp/repro.cql <<'CQL'
{cql.strip()}
CQL
CQLSH=$(command -v cqlsh || true)
[ -n "$CQLSH" ] || CQLSH=/opt/cassandra/bin/cqlsh
"$CQLSH" --request-timeout=90{protocol_args} -f /tmp/repro.cql
"""
        return self.sh(script, timeout=timeout)

    def observe_bug(self) -> str:
        raise NotImplementedError

    def last_observe_output(self) -> str:
        output = getattr(self, "_last_observe_output", None)
        if output is None:
            output = self.observe_bug()
            self._last_observe_output = output
        return output

    def bug_present(self, output: str) -> bool:
        if not self.bug_pattern:
            return False
        matched = re.search(self.bug_pattern, output, re.MULTILINE | re.DOTALL) is not None
        return not matched if self.bug_pattern_absent else matched

    def signal(self, output: str) -> str:
        if self.bug_pattern:
            match = re.search(self.bug_pattern, output, re.MULTILINE | re.DOTALL)
            if match:
                return match.group(0).strip()[:1000]
        return output.strip()[:1000]

    @mark_fault_injected
    def inject_fault(self):
        output = self.observe_bug()
        self._last_observe_output = output
        logger.info("[%s] observe output:\n%s", self.__class__.__name__, output[:4000])
