"""CASSANDRA-13874: nodetool setcachecapacity silently no-ops when the row cache is disabled.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-13874

Title: nodetool setcachecapacity behaves oddly when cache disabled.

Buggy versions (empirically confirmed in this repo's reproduction log): 4.0.0 and 4.0.1.
First fixed build: 4.0.2 (the fix guard is ABSENT from the shipped apache-cassandra-4.0.0.jar
and apache-cassandra-4.0.1.jar and first PRESENT in 4.0.2 per a bytecode contrast on
NopCacheProvider$NopCache.class). NOTE: Jira lists fixVersion 4.0.1, but the 4.0.1 Docker
image does NOT actually contain the fix — the commit landed in a later 4.0.x build (4.0.2).
Other fixVersions: 3.11.12, 4.1-alpha1, 4.1.

Reproduction (single node, stock config — row cache is disabled by default,
row_cache_size_in_mb=0, so no cassandra.yaml override is needed):
  1. Boot stock cassandra:4.0.0 (row cache disabled at startup, capacity 0 MBs).
  2. Run `nodetool setcachecapacity 200 50 50` (key=200MB, row=50MB, counter=50MB),
     asking to enable a 50 MB row cache.
  3. The command exits 0 with NO error and NO stderr; `nodetool info` shows Key Cache and
     Counter Cache capacities DID update (49->200 MiB and 24->50 MiB) while the Row Cache
     capacity SILENTLY stays at 0 bytes — the requested row cache is never enabled and no
     error is raised. This is a wrong-result/silent-no-op bug observed via `nodetool info`,
     not a query-time exception.

Verbatim buggy signature (the wrong-result `nodetool info` line after
`nodetool setcachecapacity 200 50 50` returned EXIT 0 with empty stderr):

  Row Cache              : entries 0, size 0 bytes, capacity 0 bytes, 0 hits, 0 requests, NaN recent hit rate, 0 save period in seconds
"""

import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoCassandra13874(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.0.0"
    source_git_ref = "cassandra-4.0.0"
    # 4.0.0 already ships the bug, so deploy the stock image instead of an ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/cache/NopCacheProvider.java"
    root_cause_description = (
        "When the row cache is disabled, Cassandra uses NopCacheProvider whose inner "
        "NopCache.setCapacity(long) is a silent no-op for the buggy build. As a result, "
        "`nodetool setcachecapacity <key> <row> <counter>` with a non-zero row capacity "
        "silently fails to enable the row cache and raises NO error: the Key Cache and "
        "Counter Cache capacities are updated while the Row Cache capacity stays at 0 bytes. "
        "The fix adds a guard to NopCacheProvider$NopCache.setCapacity(long) that throws "
        'UnsupportedOperationException("Setting capacity of NopCache is not permitted as '
        'this cache is disabled. Check your yaml settings if you want to enable it.") for any '
        "non-zero requested capacity, so operators get a clear error instead of a silent no-op."
    )

    # This is a nodetool-driven, wrong-result bug. The wrong value is observed via
    # `nodetool info`, not a CQL result, and `nodetool` must run on the Cassandra node
    # itself (not from a cqlsh client pod). So inject_fault() is fully overridden to run
    # the nodetool steps via `kubectl exec`, and the standard cqlsh-based continuous
    # reproducer is intentionally NOT used (continuous_reproducer stays False; a cqlsh
    # probe grepping `nodetool info` output would never see the value and would report a
    # bogus signal). The `reproducer` string below documents the buggy steps verbatim;
    # because inject_fault() is overridden and does NOT call super(), it is never piped
    # to cqlsh.
    reproducer = """
# Single-node stock cassandra:4.0.0 (row cache disabled by default: row_cache_size_in_mb=0).
# 1. PRE-CHECK — Row Cache capacity is 0 bytes:
nodetool info
# 2. Ask to enable a 50 MB row cache (key=200MB, row=50MB, counter=50MB):
nodetool setcachecapacity 200 50 50
# 3. POST-CHECK — command exited 0 with no error, but Row Cache capacity is STILL 0 bytes
#    (Key Cache 49->200 MiB and Counter Cache 24->50 MiB DID update; the row cache silently
#    did nothing):
nodetool info
"""
    # continuous_reproducer stays False (default): a cqlsh-based mitigation probe cannot run
    # nodetool nor read `nodetool info` output, so there is no usable readiness probe for this
    # bug. Diagnosis is graded by the LLMAsAJudgeOracle on the root cause.
    continuous_reproducer = False

    # The nodetool command and the wrong value it produces (silent no-op row cache).
    _SET_CACHE_CMD = "nodetool setcachecapacity 200 50 50"
    _INFO_CMD = "nodetool info"

    def _server_pod(self) -> str | None:
        """Return the name of one Cassandra server pod, or None if not found."""
        result = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-l app.kubernetes.io/instance=cassandra-{self.app.cluster_name} "
            f"-o jsonpath='{{.items[0].metadata.name}}'",
            shell=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip().strip("'") or None

    @mark_fault_injected
    def inject_fault(self):
        """Activate the buggy binary, then drive the nodetool reproducer via kubectl exec.

        4.0.0 ships the bug in the stock image (prebuilt_from_stock), so this swap is
        lifecycle-consistent even though the cluster is already buggy from boot. We then
        exec `nodetool setcachecapacity 200 50 50` (asking for a 50 MB row cache) on the
        Cassandra node and capture `nodetool info` before/after. On the buggy build the
        command exits 0 with no error and the Row Cache capacity stays at 0 bytes — the
        operator-visible wrong result this bug is about. We do NOT call super().inject_fault()
        because the base would pipe `self.reproducer` to cqlsh (which cannot run nodetool).
        """
        logger.info(f"[AutoCassandra13874] Injecting fault: ensuring buggy image active: {self._custom_image}")
        try:
            self.app.inject_buggy_image(self._custom_image)
            logger.info("[AutoCassandra13874] Buggy image active")
        except Exception as e:
            logger.warning(f"[AutoCassandra13874] inject_buggy_image raised (continuing): {e}")

        pod = self._server_pod()
        if not pod:
            logger.warning("[AutoCassandra13874] No Cassandra server pod found — skipping nodetool reproducer")
            return

        def _exec(cmd: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                f"kubectl exec -n {self.namespace} {pod} -c cassandra -- {cmd}",
                shell=True,
                capture_output=True,
                text=True,
            )

        # PRE-CHECK: row cache disabled (capacity 0 bytes).
        pre = _exec(self._INFO_CMD)
        logger.info(f"[AutoCassandra13874] PRE nodetool info:\n{pre.stdout}")

        # Ask to enable a 50 MB row cache. On the buggy build this is a silent no-op (exit 0).
        logger.info(f"[AutoCassandra13874] Running: {self._SET_CACHE_CMD}")
        setres = _exec(self._SET_CACHE_CMD)
        logger.info(
            f"[AutoCassandra13874] setcachecapacity exit={setres.returncode} "
            f"stdout={setres.stdout.strip()!r} stderr={setres.stderr.strip()!r}"
        )

        # POST-CHECK: on the buggy build the Row Cache capacity is STILL 0 bytes
        # (Key/Counter caches DID update) and no error was raised.
        post = _exec(self._INFO_CMD)
        logger.info(f"[AutoCassandra13874] POST nodetool info:\n{post.stdout}")
        logger.info(
            "[AutoCassandra13874] Buggy behavior: setcachecapacity returned exit 0 with no error "
            "but the requested 50 MB row cache was silently NOT applied (Row Cache capacity stays 0 bytes)."
        )
