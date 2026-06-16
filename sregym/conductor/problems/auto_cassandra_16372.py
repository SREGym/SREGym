"""CASSANDRA-16372: cqlsh COPY FROM drops a row whose collection contains an empty string.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16372
Buggy: 3.11.9  ->  Fixed: 3.11.10 (also 3.0.24, 4.0-rc1, 4.0).
Component: Tool/cqlsh (the fix lives in pylib/cqlshlib/copyutil.py — a cqlsh CLIENT library).

THE BUG (single cqlsh session; the buggy code is the cqlsh BINARY, not the Cassandra server):
  cqlsh COPY FROM (CSV import) treats an empty string as the null marker even when it appears as
  an element INSIDE a collection (e.g. ``list<text>``). Round-tripping a row whose list contains
  an empty-string element through ``COPY ... TO`` then ``COPY ... FROM`` raises a ParseError and
  SILENTLY DROPS the row, so the re-imported table ends up empty — data loss on an export/import
  round-trip.

HOW THE RAW-RING HARNESS MAKES THIS RUNNABLE (the former blocker, resolved):
  The previous stub was diagnosis-only because SREGym's standard reproducer runs cqlsh from a
  hardcoded ``cassandra:4.1`` client pod whose cqlsh ALREADY contains the fix, so swapping in the
  buggy 3.11.9 server image never exercised the buggy cqlsh. ``CassandraRawRingApplication``
  instead deploys the STOCK ``cassandra:3.11.9`` image, whose IN-POD cqlsh (5.0.1) IS the buggy
  client. Running the COPY round-trip via ``kubectl exec ... cqlsh`` inside ``cass-0`` exercises
  the buggy ``copyutil.py`` directly. The only signal is the cqlsh COPY FROM error line, so
  ``CassandraLogGrepOracle`` runs the reproducer as a ``command`` on ``cass-0`` and greps its
  output for the verbatim ParseError.

Verbatim buggy signature (cassandra:3.11.9, captured by hand and through the framework; note the
double space before "given up"):
  <stdin>:8:Failed to import 1 rows: ParseError - Failed to parse ['But if you now try to wash your hands,', ''] : Empty values are not allowed,  given up without retries

A/B control on fixed cassandra:3.11.10: the identical round-trip re-imports the row intact
("1 rows imported", final SELECT = 1 row, exit 0) with NO ParseError, so the same oracle grades
the bug ABSENT — confirming it discriminates buggy vs fixed.
"""

import base64
import logging
import time

from sregym.conductor.oracles.cassandra_raw_ring_oracles import CassandraLogGrepOracle
from sregym.conductor.problems.cassandra_raw_ring import CassandraRawRingProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_KS = "repro_16372"
_TABLE = f"{_KS}.test_1"
_CSV = "/var/lib/cassandra/ctm.csv"
_CQL_FILE = "/var/lib/cassandra/repro_16372.cql"

# 8-statement reproducer (COPY FROM is the 7th statement; the ParseError reports ``<stdin>:8``,
# matching the documented verbatim signature). Idempotent — CREATE IF NOT EXISTS + TRUNCATE +
# fixed-UUID INSERT, with COPY TO then COPY FROM inside one cqlsh session — so the oracle can
# re-run it on every evaluation: buggy 3.11.9 always raises the ParseError, fixed 3.11.10 always
# re-imports cleanly.
_REPRO_CQL = (
    "CREATE KEYSPACE IF NOT EXISTS " + _KS + " WITH replication = "
    "{'class':'SimpleStrategy','replication_factor':1};\n"
    "CREATE TABLE IF NOT EXISTS " + _TABLE + " ( uid uuid PRIMARY KEY, texts list<text> );\n"
    "TRUNCATE TABLE " + _TABLE + ";\n"
    "INSERT INTO " + _TABLE + " (uid, texts) VALUES "
    "(833fee3f-d4f9-418b-9387-84ac2cda5cb7, ['But if you now try to wash your hands,', '']);\n"
    "COPY " + _TABLE + " (uid, texts) TO '" + _CSV + "';\n"
    "TRUNCATE TABLE " + _TABLE + ";\n"
    "COPY " + _TABLE + " (uid, texts) FROM '" + _CSV + "';\n"
    "SELECT * FROM " + _TABLE + ";"
)

# The verbatim cqlsh copyutil ParseError. The ``.*`` spans the row payload so the regex needs no
# bracket escaping; the double space before "given up" is part of the signature.
_BUGGY_PATTERN = r"Failed to import \d+ rows: ParseError.*Empty values are not allowed,  given up without retries"


class AutoCassandra16372(CassandraRawRingProblem):
    """cqlsh COPY-FROM empty-string-in-collection data-loss bug, reproduced on a stock 3.11.9 pod.

    Realised through the benchmark architecture: ``deploy_app`` stands up a stock
    ``cassandra:3.11.9`` single-node ring; ``inject_fault`` runs the COPY round-trip once on the
    buggy in-pod cqlsh; the ``CassandraLogGrepOracle`` re-runs the round-trip and grades the bug
    as present when cqlsh emits the COPY FROM ParseError (buggy 3.11.9) vs absent when the row
    re-imports cleanly (fixed 3.11.10).
    """

    db_name = "cassandra"
    db_version = "3.11.9"
    cassandra_version = "3.11.9"
    source_git_ref = "cassandra-3.11.9"
    ring_namespace = "cassraw-16372"
    # Client-side cqlsh bug — a single in-pod cqlsh exercises it; no multi-node ring needed.
    replicas = 1

    root_cause_file = "pylib/cqlshlib/copyutil.py"
    root_cause_description = (
        "cqlsh's COPY FROM (CSV import) rejects an empty string when it appears as an element "
        "inside a collection (e.g. list<text>). In pylib/cqlshlib/copyutil.py the value-conversion "
        "path treats an empty value as the null marker and raises ParseError 'Empty values are not "
        "allowed', so the whole row fails to import and is silently dropped. The fix distinguishes "
        "empty strings from nulls for VARCHAR-typed collection elements (checking the element type "
        "is not a VarcharType before treating an empty value as null), so empty-string list "
        "elements round-trip through COPY TO / COPY FROM intact. Component: Tool/cqlsh. Fixed in "
        "3.11.10 / 3.0.24 / 4.0-rc1 / 4.0."
    )

    _POD = "cass-0"

    def _wait_cql_ready(self, timeout: int = 180) -> bool:
        """Poll until the in-pod cqlsh native transport accepts a query.

        ``wait_ring`` only confirms gossip UN; the CQL native transport (port 9042) comes up a
        little later, so the COPY round-trip must wait for it or it would hit "Connection
        refused". Returns True once ``SELECT now()`` succeeds (the ``system.now()`` header shows).
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            out = self.app.exec(self._POD, "cqlsh -e 'SELECT now() FROM system.local' 2>&1")
            if "system.now()" in out:
                return True
            time.sleep(5)
        return False

    def _repro_command(self) -> str:
        """Self-contained bash: (re)write the reproducer CQL into the pod and run it via cqlsh.

        base64 avoids all shell/CQL quoting issues; ``2>&1`` folds the COPY FROM ParseError
        (stderr) into the captured output the oracle greps.
        """
        b64 = base64.b64encode(_REPRO_CQL.encode()).decode()
        return f"echo {b64} | base64 -d > {_CQL_FILE} && cqlsh < {_CQL_FILE} 2>&1"

    @mark_fault_injected
    def inject_fault(self):
        """Run the COPY round-trip once on the buggy in-pod cqlsh and log the buggy signature."""
        if not self._wait_cql_ready():
            logger.warning("[16372] inject_fault: cqlsh native transport not ready within timeout")
        out = self.app.exec(self._POD, self._repro_command())
        logger.info(f"[16372] inject_fault COPY round-trip output:\n{out}")

    def build_mitigation_oracle(self):
        return CassandraLogGrepOracle(
            problem=self,
            pod=self._POD,
            source="command",
            command=self._repro_command(),
            pattern=_BUGGY_PATTERN,
            attempts=3,
            retry_delay=8.0,
        )
