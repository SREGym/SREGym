"""CASSANDRA-18935: fix nodetool enable/disablebinary to correctly set rpc (RpcReady).

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-18935
Buggy: 4.1.3   ->   Fixed: 4.1.4  (fixVersions include 4.1.4)

Reproduction (single-node-style, config-gated on a JVM ``-D`` flag + nodetool sequence):
  1. Start the node with native (binary) transport OFF at startup, via the JVM flag
     ``-Dcassandra.start_native_transport=false``. On the K8ssandra operator deployment this is
     injected by adding it to the K8ssandraCluster CR ``jvmOptions.additionalJvmServerOptions``
     (rendered into ``jvm-server.options`` on Cassandra 4.x), which is the operator's source of
     truth, so it survives the operator-driven rolling restart (a direct StatefulSet env patch is
     reconciled away). ``cassandra.yaml`` keeps ``start_native_transport: true``, so
     CassandraDaemon.setup() still constructs the nativeTransportService (required for
     ``enablebinary`` to work), while CassandraDaemon.start() skips the
     ``startNativeTransport(); setRpcReady(true);`` branch. The startup log confirms: "Not starting
     native transport as requested. Use JMX (StorageService->startNativeTransport()) or nodetool
     (enablebinary) to start it".
  2. ``nodetool enablebinary``  -> native (binary) transport starts and ``nodetool statusbinary``
     reports "running", but ``StorageService.setRpcReady(true)`` is NEVER called (the bug).
  3. A plain (non-counter) INSERT succeeds (the node is otherwise healthy), but a counter
     UPDATE fails: since CASSANDRA-13043 a counter update requires RpcReady=true to select a
     counter leader, and with RpcReady never set no replica is counted as "alive".

Root cause (CassandraDaemon.java startup if-block — quoted from the Jira body):
    if ((nativeFlag != null && Boolean.parseBoolean(nativeFlag))
            || (nativeFlag == null && DatabaseDescriptor.startNativeTransport())) {
        startNativeTransport();
        StorageService.instance.setRpcReady(true);
    }
``setRpcReady(true)`` only runs when native transport is enabled AT STARTUP. Starting with
native OFF and later running ``nodetool enablebinary`` starts the transport but leaves
RpcReady=false forever. The fix moves ``setRpcReady(true)`` out of this startup ``if`` (and into
enable/disablebinary) so toggling the binary transport correctly updates RpcReady.

VERBATIM BUGGY SIGNATURE (literal cqlsh output of the counter UPDATE on buggy 4.1.3):
  <stdin>:1:NoHostAvailable: ('Unable to complete the operation against any hosts', {<Host: 127.0.0.1:9042 dc1>: Unavailable('Error from server: code=1000 [Unavailable exception] message="Cannot achieve consistency level ONE" info={\\'consistency\\': \\'ONE\\', \\'required_replicas\\': 1, \\'alive_replicas\\': 0}')})

Note ``alive_replicas: 0`` while ``required_replicas: 1`` on an otherwise healthy ring with
RF=1: no replica is counted as RPC-ready for counter-leader selection — the precise symptom of
RpcReady never being set. On fixed 4.1.4 the IDENTICAL gating + nodetool sequence makes the
counter UPDATE succeed (the counter increments to c=1).

Shape: nodetool-sequence (config-gated on the ``start_native_transport=false`` startup JVM
``-D`` flag — NOT a ``cassandra.yaml`` block, and NOT a startup crash). The gating precondition
must be present at process startup, the trigger and operator-visible signature come from running
``nodetool enablebinary`` followed by a counter UPDATE, and none of that is expressible via the
shared CQL-only ``reproducer`` machinery. So we override ``inject_fault()`` to: set the startup
flag on EVERY node via the K8ssandraCluster CR ``jvmOptions`` (operator rolls every node out
native-OFF — the swap that ``inject_buggy_image`` would do is skipped because stock 4.1.3 already
IS the buggy build, and the swap only degrades the ring), wait for the ring to restabilize all-UN
with native-off confirmed by each node's startup log, run ``nodetool enablebinary`` on every node,
seed the schema + a plain write (authed, retried until the post-restart auth cache warms), capture
the GENUINE server-emitted counter-UPDATE ``alive_replicas: 0`` error via authed cqlsh, and deploy
a continuous reproducer that loops ONLY the counter UPDATE.

``continuous_reproducer`` is True. Unlike the closely-related CASSANDRA-17752 (join_ring=false),
gating native transport OFF does NOT prevent the node from joining the ring: the node reaches the
NORMAL state, only CQL/native is off at startup. Gating ALL nodes therefore yields a HEALTHY ring
in which cqlsh connects and plain writes succeed, but counter UPDATEs fail on EVERY coordinator
(RpcReady=false everywhere). The DC-wide continuous-reproducer probe is thus a correct oracle (it
fails uniformly while the bug is present), not the false-passing probe that forced 17752 to
disable it. This requires inject_fault to gate every node (see the all-nodes loops below).
"""

import json
import logging
import shlex
import subprocess
import time

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoCassandra18935(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.1.3"
    source_git_ref = "cassandra-4.1.3"
    # 4.1.3 already ships the bug (fix landed in 4.1.4), so deploy the stock image
    # instead of running a ~30-min ant-jar source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/service/CassandraDaemon.java"
    root_cause_description = (
        "CassandraDaemon only calls StorageService.setRpcReady(true) inside the startup if-block "
        "that starts native transport (gated on -Dcassandra.start_native_transport / "
        "DatabaseDescriptor.startNativeTransport()). If the node is started with native transport "
        "OFF and the binary transport is later turned on with `nodetool enablebinary`, the native "
        "transport starts but setRpcReady(true) is never called, so RpcReady stays false. Since "
        "CASSANDRA-13043 a counter update requires RpcReady=true to select a counter leader, so the "
        "counter UPDATE fails to find any RPC-ready (alive) replica and reports 'Cannot achieve "
        "consistency level ONE' with alive_replicas: 0. The fix makes nodetool enable/disablebinary "
        "correctly set RpcReady instead of leaving it pinned to its startup value."
    )

    # Error/throw bug (NOT wrong-result): the counter UPDATE fails with NoHostAvailable /
    # Unavailable ("Cannot achieve consistency level ONE", alive_replicas: 0). No incorrect value
    # is returned or persisted, so expected_output stays None. With expected_output=None the
    # ReproducerPodMitigationOracle uses expect_unready=False -> NotReady = bug present.
    expected_output = None

    # The node starts fine (native transport is simply off until enablebinary); it does NOT crash
    # on startup, so leave crash_on_startup at its False default (otherwise inject_fault would wait
    # for a CrashLoopBackOff that never happens).
    crash_on_startup = False

    # The bug fails uniformly across all coordinators once every node is gated native-OFF, so the
    # DC-wide CQL probe is a correct oracle. Deploy the continuous reproducer (counter UPDATE loop).
    continuous_reproducer = True

    # JVM flag that starts the node with native (binary) transport OFF so the buggy startup path is
    # taken (setRpcReady(true) skipped) while the nativeTransportService is still constructed.
    _NATIVE_OFF_ENV = "-Dcassandra.start_native_transport=false"

    # Startup-log line that confirms the native-off precondition actually took effect at process
    # start (the honesty guard for _wait_ring_native_off — we only proceed when this is present).
    _NATIVE_OFF_LOG = "Not starting native transport as requested"

    # K8ssandraCluster CR path that the operator renders into jvm-server.options. additionalJvm
    # ServerOptions -> jvm-server.options is valid on Cassandra 4.x (this problem is 4.1.3); the
    # validating webhook rejects it on 3.11.x. Patching the CR (the operator's source of truth)
    # STICKS across the operator-driven rolling restart, unlike a direct StatefulSet env patch
    # which the K8ssandra operator reconciles away.
    _CR_JVM_OPTS_PATH = "/spec/cassandra/datacenters/0/config/jvmOptions/additionalJvmServerOptions"

    # Keyspace / tables (mirrors the evidence-log buggy run: keyspace repro18935_ks, counter table
    # `cnt` with counter column `c`, plain table `plain`). RF=1 keeps the counter-leader symptom
    # (required_replicas: 1, alive_replicas: 0) on the otherwise-healthy ring.
    _KEYSPACE = "repro18935_ks"
    _SETUP_CQL = (
        "CREATE KEYSPACE IF NOT EXISTS repro18935_ks "
        "WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};\n"
        "CREATE TABLE IF NOT EXISTS repro18935_ks.plain (k text PRIMARY KEY, v text);\n"
        "CREATE TABLE IF NOT EXISTS repro18935_ks.cnt (k text PRIMARY KEY, c counter);\n"
        "INSERT INTO repro18935_ks.plain (k, v) VALUES ('a', 'hello');\n"
    )
    # Pure-CQL counter UPDATE looped by the continuous reproducer pod. Fully-qualified (no USE):
    # _strip_sql_db_setup strips USE statements but not CREATE KEYSPACE, so the loop string is
    # self-contained. This is the statement that fails on buggy 4.1.3 and succeeds on fixed 4.1.4.
    _COUNTER_UPDATE = (
        "CREATE KEYSPACE IF NOT EXISTS repro18935_ks "
        "WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};\n"
        "CREATE TABLE IF NOT EXISTS repro18935_ks.cnt (k text PRIMARY KEY, c counter);\n"
        "UPDATE repro18935_ks.cnt SET c = c + 1 WHERE k = 'a';\n"
    )

    # Canonical record of the buggy reproduction steps (per the evidence log). These are nodetool
    # steps run against the Cassandra SERVER pods plus the CQL trigger — they are executed by the
    # custom inject_fault() below, NOT by the CQL-only run_reproducer machinery. The continuous
    # loop uses _COUNTER_UPDATE (pure CQL) instead of this annotated block.
    reproducer = """
# Precondition: every Cassandra node is started with native (binary) transport OFF.
#   JVM_EXTRA_OPTS=-Dcassandra.start_native_transport=false
# (cassandra.yaml keeps start_native_transport: true, so setup() still builds the
#  nativeTransportService.) Startup log confirms:
#   "Not starting native transport as requested. Use JMX
#    (StorageService->startNativeTransport()) or nodetool (enablebinary) to start it"
#   `nodetool statusbinary` => not running

nodetool enablebinary;
# -> native transport starts; `nodetool statusbinary` => running.
#    BUT StorageService.setRpcReady(true) is NEVER called (the bug) -> RpcReady stays false.

# Plain (non-counter) write SUCCEEDS — the node is healthy for normal writes:
INSERT INTO repro18935_ks.plain (k, v) VALUES ('a', 'hello');
SELECT * FROM repro18935_ks.plain;

# >>>> COUNTER UPDATE (BUG TRIGGER) — fails on buggy 4.1.3 <<<<
UPDATE repro18935_ks.cnt SET c = c + 1 WHERE k = 'a';
# -> BUGGY 4.1.3:
#      <stdin>:1:NoHostAvailable: ('Unable to complete the operation against any hosts',
#        {<Host: 127.0.0.1:9042 dc1>: Unavailable('Error from server: code=1000
#         [Unavailable exception] message="Cannot achieve consistency level ONE"
#         info={'consistency': 'ONE', 'required_replicas': 1, 'alive_replicas': 0})})
#    (no replica counted as RPC-ready for counter-leader selection because RpcReady was
#     never set). FIXED 4.1.4: the counter UPDATE succeeds and c increments to 1.
"""

    # ── Pod / StatefulSet discovery + post-rollout ring stabilization ─────────────────────────

    def _cassandra_pods(self) -> list[str]:
        """Return the names of ALL running Cassandra SERVER pods for kubectl exec."""
        out = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-l app.kubernetes.io/instance=cassandra-{self.app.cluster_name} "
            f"--no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null",
            shell=True,
            capture_output=True,
            text=True,
        ).stdout
        pods = [p.strip() for p in out.splitlines() if p.strip()]
        # K8ssandra Cassandra pods are named "<cluster>-dc1-default-sts-<n>"; skip any
        # operator/stargate/reaper helper pods that may share the instance label.
        cass = [p for p in pods if "-sts-" in p]
        return cass or pods

    def _expected_ring_size(self) -> int:
        """Sum the desired replica counts of the datacenter StatefulSet(s) = full ring size, so a
        transiently missing pod during the operator rollout doesn't make us under-count the ring."""
        out = subprocess.run(
            f"kubectl get statefulsets -n {self.namespace} "
            f"-l app.kubernetes.io/instance=cassandra-{self.app.cluster_name} "
            f"-o jsonpath='{{range .items[*]}}{{.spec.replicas}} {{end}}' 2>/dev/null",
            shell=True,
            capture_output=True,
            text=True,
        ).stdout
        total = sum(int(x) for x in out.split() if x.strip().isdigit())
        return total or 3

    def _set_native_off_via_cr(self) -> bool:
        """Add ``-Dcassandra.start_native_transport=false`` to the K8ssandraCluster's
        ``jvmOptions.additionalJvmServerOptions`` (rendered into ``jvm-server.options`` on Cassandra
        4.x) so the operator rolls out EVERY node with native (binary) transport OFF at startup —
        the buggy startup path that skips ``setRpcReady(true)``.

        We patch the operator's source of truth (the K8ssandraCluster CR) rather than the rendered
        StatefulSet env: the K8ssandra operator reconciles a direct STS env patch away, whereas a CR
        ``jvmOptions`` change is exactly what the operator renders, so it STICKS across the
        operator-driven rolling restart. (``additionalJvmServerOptions`` -> ``jvm-server.options`` is
        accepted by the validating webhook on 4.x; it is rejected on 3.11.x — this problem is 4.1.3.)
        Returns True once the CR carries the option.
        """
        patch = [{"op": "add", "path": self._CR_JVM_OPTS_PATH, "value": [self._NATIVE_OFF_ENV]}]
        res = subprocess.run(
            [
                "kubectl",
                "patch",
                "k8ssandracluster",
                self.app.cluster_name,
                "-n",
                self.namespace,
                "--type=json",
                "-p",
                json.dumps(patch),
            ],
            capture_output=True,
            text=True,
        )
        logger.info(
            f"[AutoCassandra18935] set native-off via CR jvmOptions: exit={res.returncode} "
            f"{(res.stdout + res.stderr).strip()}"
        )
        check = subprocess.run(
            f"kubectl get k8ssandracluster {self.app.cluster_name} -n {self.namespace} "
            "-o jsonpath='{.spec.cassandra.datacenters[0].config.jvmOptions.additionalJvmServerOptions}' "
            "2>/dev/null",
            shell=True,
            capture_output=True,
            text=True,
        ).stdout
        ok = self._NATIVE_OFF_ENV in check
        logger.info(f"[AutoCassandra18935] CR additionalJvmServerOptions now: {check.strip()!r} (native-off set={ok})")
        return ok

    def _wait_ring_native_off(self, timeout: int = 600) -> bool:
        """Block until the operator has rolled out the native-OFF config to EVERY node: the full
        ring is all-``UN``, every node's ``nodetool statusbinary`` reports "not running" (so native
        transport was genuinely held off at startup, BEFORE we run enablebinary), and the
        startup-log confirmation line is present on every node.

        This is the honesty guard for the precondition: if any node restarted native-ON its
        coordinator would still have RpcReady=true and the counter UPDATE could succeed while the
        bug is present (a false-passing oracle). We only return True when native-off is armed on ALL
        nodes; otherwise we time out and warn (the caller proceeds but the warning records the gap).
        """
        expected = self._expected_ring_size()
        deadline = time.time() + timeout
        while time.time() < deadline:
            pods = self._cassandra_pods()
            if len(pods) >= expected:
                status = self._exec(pods[0], "nodetool status 2>/dev/null || true").stdout
                up = sum(1 for ln in status.splitlines() if ln.strip().startswith("UN"))
                # Every node must report binary NOT running => it started native-off (pre-enablebinary).
                sb = {
                    p: self._exec(p, "nodetool statusbinary 2>/dev/null || true").stdout.strip().lower() for p in pods
                }
                all_off = bool(sb) and all(v.startswith("not running") for v in sb.values())
                if up >= expected and all_off:
                    # Honesty guard: the startup log on every node must show the native-off message.
                    logs_ok = all(
                        self._exec(
                            p,
                            f"grep -m1 -F {shlex.quote(self._NATIVE_OFF_LOG)} "
                            "/var/log/cassandra/system.log 2>/dev/null || true",
                        ).stdout.strip()
                        for p in pods
                    )
                    logger.info(
                        f"[AutoCassandra18935] ring native-off stable: {up}/{expected} UN, "
                        f"all statusbinary=not running, startup-log confirmed={logs_ok}"
                    )
                    if logs_ok:
                        return True
            time.sleep(10)
        logger.warning(
            f"[AutoCassandra18935] ring did not reach {expected} UN + native-off within {timeout}s "
            "— proceeding anyway (precondition may be incompletely armed)"
        )
        return False

    def _exec(self, pod: str, inner_cmd: str) -> subprocess.CompletedProcess:
        """Run a shell command inside the cassandra container of `pod`.

        Mirrors auto_cassandra_15191's proven helper: the argv (list) form is used rather than
        ``shell=True`` + ``{cmd!r}`` so a command mixing single and double quotes (CQL maps like
        ``{'class':...}``) is passed verbatim as one argument to ``bash -lc``. Superuser auth flags
        (``-u``/``-p`` from the ``<cluster>-superuser`` secret) are regex-injected into every
        ``cqlsh`` token by the base ``_authed_cqlsh`` (bare cqlsh fails AuthenticationFailed under
        the PasswordAuthenticator); ``nodetool`` tokens are left untouched.
        """
        inner_cmd = self._authed_cqlsh(inner_cmd)
        return subprocess.run(
            ["kubectl", "exec", "-n", self.namespace, pod, "-c", "cassandra", "--", "bash", "-lc", inner_cmd],
            capture_output=True,
            text=True,
        )

    def _seed_with_retry(self, pod: str, retries: int = 30, delay: int = 5) -> bool:
        """Seed the keyspace/tables + a plain (non-counter) INSERT via authed cqlsh, retrying until
        the superuser auth cache warms.

        Right after the operator's native-off rolling restart the system_auth caches are cold and a
        superuser login transiently fails with ``AuthenticationFailed`` (Finding: auth-cache timing).
        We retry the seed until it lands so the precondition genuinely arms before we trigger/capture
        the counter UPDATE. The plain INSERT itself succeeds even with RpcReady=false (only COUNTER
        writes need an RPC-ready counter leader), so a non-error result means auth has settled.
        """
        cql = self._SETUP_CQL.replace("\n", " ").strip()
        for i in range(1, retries + 1):
            r = self._exec(pod, f"cqlsh 127.0.0.1 -e {shlex.quote(cql)} 2>&1 || true")
            out = r.stdout + r.stderr
            if not any(tok in out for tok in ("AuthenticationFailed", "Unable to connect", "NoHostAvailable")):
                logger.info(f"[AutoCassandra18935] seed succeeded on attempt {i}")
                return True
            logger.info(f"[AutoCassandra18935] seed attempt {i}/{retries} not ready: {out.strip()[:160]}")
            time.sleep(delay)
        logger.warning("[AutoCassandra18935] seed did not succeed within retries (auth cache may be cold)")
        return False

    def _capture_signature(self, pod: str) -> bool:
        """Fire the counter UPDATE via authed cqlsh on `pod` and capture the GENUINE server-emitted
        Unavailable error.

        The discriminating, root-cause signature is ``alive_replicas: 0`` (with ``required_replicas:
        1`` on an otherwise-healthy RF=1 ring) inside the server's ``Unavailable`` / "Cannot achieve
        consistency level ONE" response — no replica is counted RPC-ready for counter-leader
        selection because ``setRpcReady(true)`` was never called. We run PURE CQL (no ``#`` comments),
        so a match cannot come from echoed reproducer text — it is the real server error. Logs a
        ``*** MANIFESTED ***`` line and returns True when the signature is present.
        """
        cql = self._COUNTER_UPDATE.replace("\n", " ").strip()
        r = self._exec(pod, f"cqlsh 127.0.0.1 -e {shlex.quote(cql)} 2>&1 || true")
        out = (r.stdout + r.stderr).strip()
        logger.info(f"[AutoCassandra18935] counter UPDATE output on {pod}:\n{out}")
        discriminating = "alive_replicas" in out and ("Cannot achieve consistency level" in out or "Unavailable" in out)
        if discriminating:
            line = next((ln for ln in out.splitlines() if "alive_replicas" in ln), out)
            logger.info(
                "[AutoCassandra18935] *** MANIFESTED *** CASSANDRA-18935 counter UPDATE Unavailable "
                f"(RpcReady never set after enablebinary): {line.strip()}"
            )
            return True
        logger.warning("[AutoCassandra18935] counter UPDATE did NOT surface the alive_replicas:0 Unavailable signature")
        return False

    @mark_fault_injected
    def inject_fault(self):
        """Inject CASSANDRA-18935 on the live (buggy 4.1.3) ring: bring every node up with native
        (binary) transport OFF at startup (via the operator's CR jvmOptions), re-enable it with
        ``nodetool enablebinary`` (which on buggy 4.1.3 leaves RpcReady=false), seed the schema + a
        plain write, capture the genuine counter-UPDATE ``alive_replicas: 0`` signature, then deploy
        the continuous reproducer that loops ONLY the counter UPDATE.

        Steps:
          1. Ensure the operator is up (so the K8ssandraCluster patch is admitted + rendered).
          2. Set ``-Dcassandra.start_native_transport=false`` on the CR so the operator rolls out
             every node native-OFF; wait for the ring to be all-UN with native-off armed + confirmed.
          3. ``nodetool enablebinary`` on EVERY node -> native transport up, RpcReady still false.
          4. Seed the keyspace/tables + a plain INSERT (authed, retry until the auth cache warms).
          5. Capture the counter UPDATE failure (verbatim ``alive_replicas: 0`` from the server).
          6. Deploy the continuous counter-UPDATE reproducer (the mitigation oracle probe).
        """
        # Stock 4.1.3 IS the buggy version (the fix landed in 4.1.4), so the buggy binary is already
        # running after deploy. The documented fault is a STARTUP precondition
        # (-Dcassandra.start_native_transport=false) + `nodetool enablebinary`, NOT an image change,
        # so we deliberately do NOT call inject_buggy_image: that swaps in a re-tag that is
        # byte-identical to the deployed 4.1.3 stock image while scaling every operator Deployment to
        # 0 — which takes down the pod webhook and degrades the ring (the 15191 Finding #6/#22
        # failure mode). Keeping the operator UP lets the CR jvmOptions patch below be admitted by the
        # validating webhook and rolled out cleanly in a single operator-driven restart.
        logger.info(
            "[AutoCassandra18935] stock 4.1.3 is the buggy version; injecting via startup native-off "
            "(CR jvmOptions) + nodetool enablebinary (no image swap needed)"
        )

        # 1. Make sure the operator is up so the K8ssandraCluster patch is admitted and rendered.
        try:
            self.app._scale_operator_up()
        except Exception as e:  # noqa: BLE001 - best-effort, operator is normally already up
            logger.warning(f"[AutoCassandra18935] _scale_operator_up raised: {e}")

        # 2. Native transport OFF at startup on EVERY node, via the operator's source of truth, then
        #    wait for the operator to roll it out (all-UN, statusbinary=not running, startup-log).
        if not self._set_native_off_via_cr():
            logger.warning("[AutoCassandra18935] could not set native-off in the CR — precondition may not arm")
        armed = self._wait_ring_native_off()

        pods = self._cassandra_pods()
        if not pods:
            logger.warning("[AutoCassandra18935] no Cassandra pods after native-off rollout — aborting inject")
            return

        # 3. Re-enable the binary transport on EVERY node. On buggy 4.1.3 this starts native but
        #    leaves RpcReady=false (the bug); statusbinary should flip to "running" while RpcReady
        #    stays unset, so every coordinator fails the counter UPDATE uniformly.
        for pod in pods:
            en = self._exec(pod, "nodetool enablebinary 2>&1 || true")
            sb = self._exec(pod, "nodetool statusbinary 2>&1 || true")
            logger.info(
                f"[AutoCassandra18935] {pod} enablebinary exit={en.returncode} "
                f"{(en.stdout + en.stderr).strip()}; statusbinary={sb.stdout.strip()}"
            )

        # 4. Seed the schema + a plain write (succeeds — proves the ring is otherwise healthy).
        logger.info("[AutoCassandra18935] seeding keyspace/tables + plain write (authed, with retry)")
        self._seed_with_retry(pods[0])

        # 5. Capture the GENUINE counter-UPDATE signature (server-emitted alive_replicas: 0).
        manifested = any(self._capture_signature(pod) for pod in pods)
        if armed and not manifested:
            logger.warning(
                "[AutoCassandra18935] native-off armed but counter UPDATE did not surface "
                "alive_replicas:0 — RpcReady may have been set despite native-off (investigate)"
            )

        # 6. Continuous counter-UPDATE reproducer (the mitigation oracle probe).
        logger.info("[AutoCassandra18935] deploying continuous counter-UPDATE reproducer")
        self.app.deploy_continuous_reproducer(self._COUNTER_UPDATE, self.expected_output)

    @mark_fault_injected
    def recover_fault(self):
        """Remove the native-off JVM option from the K8ssandraCluster CR (the operator rolls every
        node back with native transport — and RpcReady — set normally at startup) and restore the
        stock image."""
        res = subprocess.run(
            [
                "kubectl",
                "patch",
                "k8ssandracluster",
                self.app.cluster_name,
                "-n",
                self.namespace,
                "--type=json",
                "-p",
                json.dumps([{"op": "remove", "path": self._CR_JVM_OPTS_PATH}]),
            ],
            capture_output=True,
            text=True,
        )
        logger.info(
            f"[AutoCassandra18935] removed native-off from CR: exit={res.returncode} "
            f"{(res.stdout + res.stderr).strip()}"
        )
        logger.info("[AutoCassandra18935] recovering: restoring cluster to stock image")
        try:
            self.app.restore_stock_image(custom_image=self._custom_image)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[AutoCassandra18935] restore_stock_image raised: {e}")
        logger.info("[AutoCassandra18935] recovery complete")
