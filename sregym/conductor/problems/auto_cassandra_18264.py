"""CASSANDRA-18264: CustomClassLoader does not load jars — triggers from JARs are broken.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-18264
Buggy: 4.1.0  →  Fixed: 4.1.1 (also 5.0-alpha1 / 5.0).

Reproduction (single node):
  1. Place any .jar in the Cassandra triggers directory (/etc/cassandra/triggers) on 4.1.0.
  2. Run `nodetool reloadtriggers` (also fired by CREATE TRIGGER / first trigger use).
  3. TriggerExecutor.reloadClasses() -> new CustomClassLoader(parent, triggerDir) ->
     addClassPath() calls FileUtils.createTempFile (which PHYSICALLY creates the empty
     destination temp file) and then java.nio.file.Files.copy(inputJar, out) WITHOUT
     StandardCopyOption.REPLACE_EXISTING — so the copy throws FileAlreadyExistsException,
     breaking ALL trigger-from-JAR loading. The fix in 4.1.1 adds REPLACE_EXISTING.

Verbatim buggy signature (nodetool reloadtriggers on 4.1.0, exit code 2):
  error: /tmp/lib/cassandra-0.jar
  -- StackTrace --
  java.nio.file.FileAlreadyExistsException: /tmp/lib/cassandra-0.jar
      at java.base/java.nio.file.Files.copy(Unknown Source)
      at org.apache.cassandra.triggers.CustomClassLoader.addClassPath(CustomClassLoader.java:86)
      at org.apache.cassandra.triggers.CustomClassLoader.<init>(CustomClassLoader.java:65)
      at org.apache.cassandra.triggers.TriggerExecutor.reloadClasses(TriggerExecutor.java:64)
      at org.apache.cassandra.service.StorageProxy.reloadTriggerClasses(StorageProxy.java:2709)

Shape: nodetool/sequence (file-staging precondition + `nodetool reloadtriggers`), an
ERROR bug (throws, no wrong value). It is NOT expressible as a pure CQL reproducer string:
the failure is server-local (JMX) and needs a .jar physically present on the server node's
triggers directory. The standard continuous-reproducer mechanism spins up a SEPARATE
cqlsh client pod, which can neither place a jar on the server nor invoke nodetool, so this
is encoded as a custom inject_fault() (kubectl exec) and graded diagnosis-only
(continuous_reproducer = False, mitigation_oracle = None), mirroring cassandra_20108.
"""

import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoCassandra18264(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.1.0"
    source_git_ref = "cassandra-4.1.0"
    # 4.1.0 already ships the bug, so deploy the stock image instead of an ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/triggers/CustomClassLoader.java"
    root_cause_description = (
        "Loading a trigger from a JAR is broken in 4.1.0. CustomClassLoader.addClassPath() "
        "(CustomClassLoader.java:86) creates the destination temp file via "
        "FileUtils.createTempFile() — which physically creates the empty file — and then calls "
        "java.nio.file.Files.copy(inputJar, out) WITHOUT StandardCopyOption.REPLACE_EXISTING. "
        "Because the temp destination already exists, Files.copy throws "
        "java.nio.file.FileAlreadyExistsException (wrapped as FSWriteError), so ANY .jar in the "
        "triggers directory makes trigger (re)loading fail before any class is loaded. Earlier "
        "releases used Guava's com.google.common.io.Files (which overwrites by default); the fix "
        "in 4.1.1 adds StandardCopyOption.REPLACE_EXISTING to the Files.copy call."
    )

    # Documentation of the exact buggy steps. NOT auto-run: inject_fault() is overridden
    # below because this bug needs a server-side .jar drop + nodetool, which a cqlsh-only
    # reproducer string cannot express.
    reproducer = """
# 1) Place any .jar in the Cassandra triggers directory on the (buggy 4.1.0) server node:
mkdir -p /etc/cassandra/triggers
cp $(ls /opt/cassandra/lib/*.jar | head -1) /etc/cassandra/triggers/mytrigger.jar

# 2) Force a trigger reload (also fired by CREATE TRIGGER / first trigger use):
nodetool reloadtriggers

# Buggy 4.1.0 -> exit code 2 with:
#   java.nio.file.FileAlreadyExistsException: /tmp/lib/cassandra-0.jar
#     at org.apache.cassandra.triggers.CustomClassLoader.addClassPath(CustomClassLoader.java:86)
# Fixed 4.1.1 -> exit 0, jar copied successfully.
"""
    # Diagnosis-only: the standard continuous reproducer runs in a separate cqlsh client
    # pod that cannot stage a server-side jar or run nodetool, so no looping mitigation
    # probe can distinguish buggy vs fixed here (mirrors cassandra_20108).
    continuous_reproducer = False

    # In-pod paths (per the reproduction evidence log). The K8ssandra cassandra container
    # keeps the distribution under /opt/cassandra and config under /etc/cassandra.
    _TRIGGERS_DIR = "/etc/cassandra/triggers"
    _LIB_GLOB = "/opt/cassandra/lib/*.jar"

    def _cassandra_pod(self) -> str | None:
        """Return one Cassandra server pod name (the bug fires locally on any node)."""
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
        """Swap to the buggy image (no-op for the prebuilt 4.1.0 stock image), drop a jar
        into the server's triggers directory, then run `nodetool reloadtriggers` so the
        FileAlreadyExistsException is raised and logged to system.log.
        """
        if not getattr(self, "_predeployed_buggy", False):
            logger.info(f"[AutoCassandra18264] Injecting fault: swapping cluster to {self._custom_image}")
            self.app.inject_buggy_image(self._custom_image)
            logger.info("[AutoCassandra18264] Buggy image active")
        else:
            logger.info("[AutoCassandra18264] Buggy image already deployed — skipping swap")

        pod = self._cassandra_pod()
        if not pod:
            logger.warning("[AutoCassandra18264] No Cassandra pod found — cannot inject fault")
            return

        logger.info(f"[AutoCassandra18264] Staging a .jar in {self._TRIGGERS_DIR} on pod {pod}")
        stage = subprocess.run(
            f"kubectl exec -n {self.namespace} {pod} -c cassandra -- bash -c "
            f"'mkdir -p {self._TRIGGERS_DIR} && "
            f"cp $(ls {self._LIB_GLOB} | head -1) {self._TRIGGERS_DIR}/mytrigger.jar'",
            shell=True,
            capture_output=True,
            text=True,
        )
        if stage.returncode != 0:
            logger.warning(
                f"[AutoCassandra18264] Staging jar failed (rc={stage.returncode}): {stage.stderr.strip()[:300]}"
            )

        logger.info("[AutoCassandra18264] Running `nodetool reloadtriggers` (expect failure)")
        reload = subprocess.run(
            f"kubectl exec -n {self.namespace} {pod} -c cassandra -- nodetool reloadtriggers",
            shell=True,
            capture_output=True,
            text=True,
        )
        # Buggy 4.1.0 exits non-zero with FileAlreadyExistsException; this is the bug firing.
        logger.info(
            f"[AutoCassandra18264] nodetool reloadtriggers exit={reload.returncode}; "
            f"output: {(reload.stdout + reload.stderr).strip()[:400]}"
        )
