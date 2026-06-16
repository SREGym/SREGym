"""Base class for Cassandra source-code bug problems.

These problems deploy a Cassandra cluster at a specific (buggy) version,
clone the source code for agent inspection, inject a fault by triggering
the bug via CQL, and evaluate whether the agent correctly identifies
the root cause in the source code.
"""

import logging
from pathlib import Path

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.cassandra import Cassandra
from sregym.service.kubectl import KubeCtl
from sregym.service.source_manager import SourceManager
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

CASSANDRA_REPO_URL = "https://github.com/apache/cassandra.git"


class CassandraBugProblem(Problem):
    """Base class for problems that require finding a bug in Cassandra source code.

    Subclasses must set:
        - cassandra_version: the buggy Cassandra release (e.g. "4.1.7")
        - source_git_ref: git tag/branch/commit to checkout (e.g. "cassandra-4.1.7")
        - trigger_cql: CQL statement(s) that trigger the bug
        - root_cause_description: human-readable description of the root cause
        - root_cause_file: source file containing the bug (e.g. "src/java/.../ByteType.java")
    """

    cassandra_version: str
    source_git_ref: str
    trigger_cql: str
    root_cause_description: str
    root_cause_file: str
    allows_rebuild: bool = False  # subclasses set this to True to enable POST /cassandra/rebuild

    def requires_openebs(self) -> bool:
        """Cassandra problems require OpenEBS for persistent storage."""
        return True

    def _create_app(self) -> Cassandra:
        """Factory method — override in subclasses to customise the Cassandra deployment."""
        return Cassandra(cassandra_version=self.cassandra_version)

    def __init__(self):
        self.app = self._create_app()
        super().__init__(app=self.app, namespace=self.app.namespace)

        self.kubectl = KubeCtl()
        self.source_manager = SourceManager()

        # Clone source code on the host for bind-mounting into the agent container
        self.source_code_path = self.source_manager.ensure_source(
            repo_url=CASSANDRA_REPO_URL,
            git_ref=self.source_git_ref,
            name="cassandra",
        )

        self.root_cause = self.build_structured_root_cause(
            component=f"source/{self.root_cause_file}",
            namespace=self.namespace,
            description=self.root_cause_description,
        )

        # Diagnosis oracle: LLM judge evaluates whether the agent found the right root cause
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        # No mitigation oracle — these are source-code bugs, not infrastructure issues
        self.mitigation_oracle = None


    @mark_fault_injected
    def inject_fault(self):
        """Trigger the bug by executing the CQL statements that reproduce it."""
        logger.info(f"[CassandraBug] Triggering bug via CQL in namespace {self.namespace}")
        try:
            result = self.app.run_cql(self.trigger_cql)
            logger.info(f"[CassandraBug] CQL trigger completed. Output: {result!r}")
        except Exception as e:
            # The trigger CQL may produce an error response from the server —
            # that error IS the bug manifesting. Log it clearly.
            logger.info(f"[CassandraBug] CQL trigger produced error (this may be expected): {e}")
        logger.info("[CassandraBug] Fault injection complete — IndexOutOfBoundsException should now be in Cassandra logs (check server-system-logger container)")

    @mark_fault_injected
    def recover_fault(self):
        """No recovery needed — the bug is in the source code, not injected state."""
        logger.info("[CassandraBug] No fault recovery needed (source-code bug)")
