"""CASSANDRA-21057: data-disk-usage guardrail cannot be disabled at runtime.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-21057

Buggy: 4.1.10  ->  Fixed: 4.1.11

Reproduction (single node, nodetool sequence):
  1. nodetool setguardrailsconfig data_disk_usage_max_disk_size 1MiB
  2. nodetool setguardrailsconfig data_disk_usage_percentage_threshold 2 1  (fail=2, warn=1)
  3. Wait one 30s DiskUsageMonitor tick -> the node advertises gossip DISK_USAGE = FULL.
  4. nodetool setguardrailsconfig data_disk_usage_percentage_threshold null null  (disable the guardrail).
On the buggy 4.1.10 build the guardrail never re-evaluates after being disabled, so the
node keeps advertising FULL. On fixed 4.1.11 the disable transitions DISK_USAGE to
NOT_AVAILABLE within one tick (the fix's onDiskUsageGuardrailDisabled callback).

VERBATIM BUGGY SIGNATURE (4.1.10):
  gossip DISK_USAGE stays FULL at 30s and 60s after disabling (node never stops advertising FULL).

NOTE ON SHAPE / ORACLE
----------------------
This is a NODETOOL-SEQUENCE bug whose only observable symptom is gossip state
(``nodetool gossipinfo`` -> DISK_USAGE), NOT a CQL result or a CQL exception.
inject_fault() is therefore overridden to drive the three ``nodetool
setguardrailsconfig`` commands via ``kubectl exec`` (cqlsh cannot run nodetool, so
the inherited GenericCustomBuildProblem.inject_fault() — which pipes ``reproducer``
into cqlsh — would silently never inject the fault). ``continuous_reproducer`` is
left False: the generic Cassandra continuous-reproducer workload runs the reproducer
through cqlsh and cannot observe gossip DISK_USAGE, so it could not distinguish
bug-present from fixed. Diagnosis-only (mitigation_oracle = None), matching the
nodetool precedent in cassandra_20108.py.
"""

import logging
import subprocess
import time

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoCassandra21057(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.1.10"
    source_git_ref = "cassandra-4.1.10"
    # 4.1.10 already ships the bug (fix landed in 4.1.11), so deploy the stock
    # image instead of running a ~30-min ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/service/disk/usage/DiskUsageMonitor.java"
    root_cause_description = (
        "The data-disk-usage guardrail cannot be disabled at runtime. In "
        "DiskUsageMonitor.java the monitor's periodic tick short-circuits with "
        "`if (!enabled) return;` and never re-evaluates disk usage after the guardrail "
        "is disabled, so the previously-computed DISK_USAGE = FULL state is never cleared "
        "and the node keeps advertising FULL via gossip. The fix adds an "
        "onDiskUsageGuardrailDisabled path that transitions the gossip state to "
        "NOT_AVAILABLE when the guardrail is turned off."
    )

    # Documentation-only here: inject_fault() runs these nodetool steps directly
    # (they are NOT CQL and cannot be piped into cqlsh). Kept verbatim from the
    # reproduction evidence log so the encoded buggy path is auditable.
    reproducer = """
nodetool setguardrailsconfig data_disk_usage_max_disk_size 1MiB
nodetool setguardrailsconfig data_disk_usage_percentage_threshold 2 1
# wait one 30s DiskUsageMonitor tick -> gossip DISK_USAGE = FULL
nodetool setguardrailsconfig data_disk_usage_percentage_threshold null null
# buggy 4.1.10: gossip DISK_USAGE stays FULL at 30s and 60s after disabling
"""
    # Gossip-only signature, not CQL-observable -> no continuous reproducer / mitigation oracle.
    continuous_reproducer = False

    # nodetool steps from the evidence log (the disable transition is the one the
    # buggy build fails to honor). Run on the first cassandra pod (single-node bug).
    _NODETOOL_STEPS = [
        "nodetool setguardrailsconfig data_disk_usage_max_disk_size 1MiB",
        "nodetool setguardrailsconfig data_disk_usage_percentage_threshold 2 1",
    ]
    _NODETOOL_DISABLE = "nodetool setguardrailsconfig data_disk_usage_percentage_threshold null null"
    # One DiskUsageMonitor tick is 30s; wait a little longer to be safe.
    _MONITOR_TICK_WAIT_S = 35

    def _cassandra_pod(self) -> str | None:
        """Return the name of the first cassandra pod in the cluster namespace."""
        out = (
            subprocess.run(
                f"kubectl get pods -n {self.namespace} -l app.kubernetes.io/name=cassandra "
                f"-o jsonpath='{{.items[0].metadata.name}}'",
                shell=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .strip("'")
        )
        return out or None

    def _exec_nodetool(self, pod: str, cmd: str) -> str:
        """Run a single nodetool command inside the cassandra container and log output."""
        result = subprocess.run(
            f"kubectl exec -n {self.namespace} {pod} -c cassandra -- {cmd}",
            shell=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        logger.info(f"[AutoCassandra21057] `{cmd}` exit={result.returncode} stdout={out[:300]!r} stderr={err[:300]!r}")
        return out

    def _log_disk_usage_state(self, pod: str, label: str) -> str:
        """Surface the decisive DISK_USAGE gossip field un-truncated.

        The documented 4.1.10 bug is that a node keeps advertising
        ``DISK_USAGE:<gen>:FULL`` after the guardrail is disabled (the fix
        transitions it to ``NOT_AVAILABLE``). The raw ``gossipinfo`` dump is
        large and ``_exec_nodetool`` truncates it before the DISK_USAGE field,
        so extract and log just those lines here.
        """
        result = subprocess.run(
            f"kubectl exec -n {self.namespace} {pod} -c cassandra -- nodetool gossipinfo",
            shell=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = result.stdout or ""
        disk_lines = [ln.strip() for ln in out.splitlines() if "DISK_USAGE" in ln]
        if disk_lines:
            logger.info(f"[AutoCassandra21057] DISK_USAGE gossip ({label}): {disk_lines}")
        else:
            logger.info(f"[AutoCassandra21057] DISK_USAGE gossip ({label}): ABSENT (node not flagged FULL this tick)")
        return out

    @mark_fault_injected
    def inject_fault(self):
        """Drive the disk-usage guardrail nodetool sequence that exposes the bug.

        The stock 4.1.10 image already contains the bug, so no image swap is
        normally needed; we mirror the base-class guard in case the cluster was
        not pre-deployed with the buggy image. We then:
          1. set a tiny max disk size + a low percentage threshold so the monitor
             flags the node as FULL,
          2. wait one ~30s monitor tick (gossip DISK_USAGE -> FULL),
          3. disable the guardrail (threshold null null).
        On 4.1.10 the node keeps advertising DISK_USAGE = FULL after the disable.
        """
        if getattr(self, "_predeployed_buggy", False):
            logger.info("[AutoCassandra21057] Buggy image already deployed — skipping swap")
        else:
            logger.info(f"[AutoCassandra21057] Swapping cluster to buggy image: {self._custom_image}")
            self.app.inject_buggy_image(self._custom_image)

        pod = self._cassandra_pod()
        if not pod:
            logger.warning("[AutoCassandra21057] No cassandra pod found — cannot run nodetool sequence")
            return

        logger.info(f"[AutoCassandra21057] Running guardrail nodetool sequence on pod {pod}")
        # NOTE: nodetool talks to the node over JMX. On a default K8ssandra deploy
        # the in-container nodetool authenticates locally; if JMX auth is enforced
        # these execs may need -u/-pw flags (runtime caveat, does not affect compile).
        for cmd in self._NODETOOL_STEPS:
            self._exec_nodetool(pod, cmd)

        logger.info(
            f"[AutoCassandra21057] Waiting {self._MONITOR_TICK_WAIT_S}s for one "
            f"DiskUsageMonitor tick (expect gossip DISK_USAGE -> FULL)"
        )
        time.sleep(self._MONITOR_TICK_WAIT_S)
        # Observe the FULL state for the log (not asserted — diagnosis-only problem).
        self._log_disk_usage_state(pod, "after-tick (expect FULL)")

        logger.info("[AutoCassandra21057] Disabling the guardrail (threshold null null)")
        self._exec_nodetool(pod, self._NODETOOL_DISABLE)

        logger.info(
            f"[AutoCassandra21057] Waiting {self._MONITOR_TICK_WAIT_S}s after disable; "
            f"buggy 4.1.10 keeps advertising DISK_USAGE = FULL"
        )
        time.sleep(self._MONITOR_TICK_WAIT_S)
        self._log_disk_usage_state(pod, "after-disable (buggy keeps FULL; fixed=NOT_AVAILABLE)")
        logger.info("[AutoCassandra21057] Guardrail nodetool sequence complete")
