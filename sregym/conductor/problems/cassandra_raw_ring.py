"""Base class for multi-node Cassandra bug problems reproduced on a raw, self-seeded ring.

Several real Cassandra bugs need orchestration the single-CQL ``GenericCustomBuildProblem``
harness cannot express: per-replica gossip isolation, a node held in BOOT via a JVM flag,
an in-pod killable ``CassandraDaemon``, or a startup launched with
``-Dcassandra.replace_address``. ``CassandraRawRingProblem`` deploys a stock buggy
``cassandra:<version>`` image as a plain headless Service + StatefulSet (``CassandraRawRingApplication``)
and exposes the per-pod ``kubectl exec`` primitives those reproductions need.

It subclasses ``GenericCustomBuildProblem`` purely so the ``auto_cassandra_*.py`` files that
use it are picked up by ``ProblemRegistry._load_auto_generated()`` (which registers any
``auto_*.py`` class that ``issubclass(GenericCustomBuildProblem)``). It deliberately does NOT
run the operator build/deploy machinery — ``__init__`` is fully overridden to stand up the
raw ring instead. Subclasses set ``cassandra_version`` / ``ring_namespace`` / ``root_cause_*``,
implement ``inject_fault`` (the multi-node dance), and return a mitigation oracle from
``build_mitigation_oracle``.
"""

import logging

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.service.apps.cassandra_raw_ring import CassandraRawRingApplication
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

CASSANDRA_REPO_URL = "https://github.com/apache/cassandra.git"


class CassandraRawRingProblem(GenericCustomBuildProblem):
    """Deploy a stock buggy Cassandra ring (operator-free) and inject a multi-node fault."""

    db_name = "cassandra"

    # ── Required in subclasses ──────────────────────────────────────────────────
    cassandra_version: str = ""  # e.g. "3.11.7" (buggy = released fix patch - 1)
    source_git_ref: str | None = None  # e.g. "cassandra-3.11.7" (for /opt/source clone)
    ring_namespace: str = "cassraw"  # dedicated namespace for this problem's ring

    # ── Ring topology / JVM ─────────────────────────────────────────────────────
    replicas: int = 2
    num_tokens: int = 16
    jvm_extra_opts: str = ""
    startup_prelude: str = ""
    hinted_handoff_enabled: bool = False
    extra_pods: list[dict] = []  # bare pods created at deploy time
    ready_timeout: int = 600
    node_name: str = ""

    # ── Diagnosis metadata ──────────────────────────────────────────────────────
    root_cause_file: str = "source"
    root_cause_description: str = ""
    # Cloning apache/cassandra (per ref) into /opt/source is opt-in: it is heavy and only
    # useful for a real code-reading agent. Diagnosis grading uses root_cause text, not the
    # tree, so leave it off by default to keep loads fast and avoid host disk pressure.
    clone_source: bool = False

    @property
    def image(self) -> str:
        return getattr(self, "image_override", "") or f"cassandra:{self.cassandra_version}"

    def __init__(self):
        if not self.cassandra_version:
            raise ValueError(f"{self.__class__.__name__} must set cassandra_version")

        app = CassandraRawRingApplication(
            image=self.image,
            namespace=self.ring_namespace,
            replicas=self.replicas,
            num_tokens=self.num_tokens,
            jvm_extra_opts=self.jvm_extra_opts,
            startup_prelude=self.startup_prelude,
            hinted_handoff_enabled=self.hinted_handoff_enabled,
            extra_pods=list(self.extra_pods),
            ready_timeout=self.ready_timeout,
            node_name=self.node_name,
        )

        # Run post_deploy() after the ring is up (subclass hook for extra setup).
        _original_deploy = app.deploy

        def _wrapped_deploy():
            _original_deploy()
            self.post_deploy()

        app.deploy = _wrapped_deploy

        # Initialise as a plain Problem — bypass GenericCustomBuildProblem.__init__'s
        # operator build/deploy path entirely.
        Problem.__init__(self, app=app, namespace=app.namespace)

        # Best-effort source clone so the agent can inspect the buggy code at /opt/source.
        self.source_code_path = None
        if self.clone_source and self.source_git_ref:
            try:
                from sregym.service.source_manager import SourceManager

                self.source_code_path = SourceManager().ensure_source(
                    repo_url=CASSANDRA_REPO_URL,
                    git_ref=self.source_git_ref,
                    name="cassandra",
                )
            except Exception as e:
                logger.warning(f"[RawRing] source clone failed (continuing without /opt/source): {e}")

        self.root_cause = self.build_structured_root_cause(
            component=f"source/{self.root_cause_file}",
            namespace=self.namespace,
            description=self.root_cause_description or f"Cassandra {self.cassandra_version} bug",
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = self.build_mitigation_oracle()

    # ── Hooks for subclasses ────────────────────────────────────────────────────

    def requires_openebs(self) -> bool:
        # Raw ring uses emptyDir volumes — no persistent storage class needed.
        return False

    def post_deploy(self):
        """Optional: run after the ring is Ready (e.g. disable handoff, create schema)."""

    def build_mitigation_oracle(self):
        """Return a mitigation oracle (CassandraWrongResultOracle / CassandraLogGrepOracle)."""
        return None

    @mark_fault_injected
    def inject_fault(self):  # pragma: no cover - subclasses override
        raise NotImplementedError(f"{self.__class__.__name__} must implement inject_fault()")

    @mark_fault_injected
    def recover_fault(self):
        """No code-level recovery — the bug is in the binary; the Conductor tears the ring down."""
        logger.info(f"[RawRing] recover_fault: no-op for {self.__class__.__name__} (ring torn down on cleanup)")
