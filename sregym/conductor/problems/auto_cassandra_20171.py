"""CASSANDRA-20171: GRANT permission on virtual keyspaces system_views / system_virtual_schema is impossible.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-20171
Buggy: 5.0.4  ->  Fixed: 5.0.5 (also 4.0.18, 4.1.9, 6.0-alpha1, 6.0).
Fix commit: 348ffb0ba09f10893e8dedfbd69c950fb129ec53
Component: Feature/Virtual Tables.

Reproduction summary (single auth-enabled node, RF=1, PasswordAuthenticator + CassandraAuthorizer):
  As the `cassandra` superuser, create a non-superuser role then GRANT a permission on each of the two
  virtual keyspaces:
    GRANT SELECT PERMISSION ON KEYSPACE system_views TO test;
    GRANT SELECT PERMISSION ON KEYSPACE system_virtual_schema TO test;
  On buggy 5.0.4 BOTH throw InvalidRequest code=2200 "Resource <keyspace ...> doesn't exist", while a
  GRANT on a real keyspace (system, system_schema) succeeds in the same session. On fixed 5.0.5 all four
  GRANTs succeed (LIST ALL PERMISSIONS returns 4 rows instead of 2).

VERBATIM BUGGY SIGNATURE (from the reproduction evidence log, on 5.0.4):
  <stdin>:1:InvalidRequest: Error from server: code=2200 [Invalid query] message="Resource <keyspace system_views> doesn't exist"
  <stdin>:1:InvalidRequest: Error from server: code=2200 [Invalid query] message="Resource <keyspace system_virtual_schema> doesn't exist"

Root cause (confirmed against the cassandra-5.0.4 source and the fix):
  DataResource.exists() resolves a KEYSPACE-level resource via Schema.instance.getKeyspaces().contains(keyspace)
  (case KEYSPACE / ALL_TABLES). The virtual keyspaces system_views and system_virtual_schema live in the
  VirtualKeyspaceRegistry and are NOT present in system_schema.keyspaces / Schema.instance.getKeyspaces(),
  so the existence check fails and GRANT ... ON KEYSPACE <virtual> is rejected with
  code=2200 "Resource <keyspace ...> doesn't exist" before the permission is ever written. The fix makes
  the keyspace-existence check recognize the virtual keyspaces so they become grantable.

Reproduction shape: config-gated, single auth-enabled node, pure CQL run AS the superuser. The bug
  requires the auth stack, and specifically CassandraAuthorizer:
    * authenticator: PasswordAuthenticator -- GRANT is only a supported operation on an authenticated,
      authz-enabled cluster (K8ssandra enables PasswordAuthenticator by default and creates the
      `<cluster>-superuser` secret);
    * authorizer: CassandraAuthorizer       -- under the operator's default AllowAllAuthorizer, GRANT
      instead fails with "...not supported by AllowAllAuthorizer", which is NOT the target signature
      (per the evidence log). Only CassandraAuthorizer routes GRANT through the DataResource existence
      check that trips this bug.
  Neither cassandra.yaml setting is CQL- or runtime-settable, and CassandraAuthorizer with
  AllowAllAuthenticator is a startup config-validation error, so both are set together. post_deploy()
  patches the operator-managed K8ssandraCluster CR's cassandraYaml block (a direct pod/StatefulSet edit
  would be reverted on the next reconcile) and waits for the operator's rolling restart. This mirrors
  auto_cassandra_16977 (the other CassandraAuthorizer-gated GRANT/permission bug).

NOTE on the reproducer/mitigation plumbing (framework limitation, NOT a defect in this encoding):
  The SHARED Cassandra reproducer plumbing in sregym/service/db_build_spec.py
  (`_cassandra_run_reproducer` and `_cassandra_reproducer_workload`) connects with a bare `cqlsh <svc>`
  and does NOT pass `-u cassandra -p cassandra`; once PasswordAuthenticator is enabled those connections
  are rejected with AuthenticationFailed before the GRANT is ever reached. This is a framework-wide
  limitation affecting every auth-gated Cassandra problem (see also auto_cassandra_16977 and
  auto_cassandra_19749), and fixing the shared helpers to pass credentials is out of scope for this
  code-generation task and cannot be statically verified. Recorded loudly per the skill's "reproducer
  validation is narrow" gotcha.

  continuous_reproducer is nevertheless True (mitigation oracle wired up), because — UNLIKE
  auto_cassandra_12525 — this reproducer is BUILD-DEPENDENT: buggy 5.0.4 throws code=2200 on the
  virtual-keyspace GRANT while fixed 5.0.5 succeeds. If the shared helpers passed credentials, the
  exit-code probe would discriminate buggy (NotReady) from fixed (Ready) correctly; the only blocker is
  the external credential gap, not the reproducer's logic. The bug is a transient exception that leaves
  NO persistent faulty state behind, so a capable agent reproduces it by reading the `<cluster>-superuser`
  secret and running credentialed `cqlsh`. No custom inject_fault is needed (everything runs as the single
  superuser identity — there is no multi-identity requirement like auto_cassandra_21219).
"""

import json
import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem

logger = logging.getLogger(__name__)


class AutoCassandra20171(GenericCustomBuildProblem):
    db_name = "cassandra"
    # Buggy version = fix patch (5.0.5) - 1. The bug already ships in the stock 5.0.4 image,
    # so deploy/re-tag the stock image instead of an ~30-min `ant jar` source build.
    db_version = "5.0.4"
    source_git_ref = "cassandra-5.0.4"
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/auth/DataResource.java"
    root_cause_description = (
        "GRANT a permission ON KEYSPACE on the virtual keyspaces system_views or system_virtual_schema "
        'fails with InvalidRequest code=2200 "Resource <keyspace ...> doesn\'t exist", even though '
        "the same GRANT succeeds on real keyspaces (system, system_schema) in the same session. The root "
        "cause is in DataResource.java: DataResource.exists() resolves a KEYSPACE-level resource via "
        "Schema.instance.getKeyspaces().contains(keyspace) (the KEYSPACE / ALL_TABLES case). The two "
        "virtual keyspaces live in the VirtualKeyspaceRegistry and are NOT listed in system_schema.keyspaces "
        "/ Schema.instance.getKeyspaces(), so the existence check returns false and the GRANT is rejected "
        "before any permission is written. The fix makes the keyspace-existence check recognize the "
        "virtual keyspaces so system_views and system_virtual_schema become grantable."
    )

    # Single auth-enabled node, pure CQL, run AS the superuser. The two virtual-keyspace GRANTs are the
    # load-bearing discriminators: buggy 5.0.4 throws code=2200 "Resource <keyspace ...> doesn't exist"
    # on each, while fixed 5.0.5 accepts them. The real-keyspace GRANTs (system, system_schema) and the
    # trailing LIST provide within-version context (they succeed on both builds; LIST returns 2 rows on
    # buggy vs 4 rows on fixed).
    #
    # CREATE ROLE IF NOT EXISTS + the idempotent GRANTs keep the block replayable: the continuous-
    # reproducer pod loops this CQL, so every iteration must (re)create the role before re-issuing the
    # GRANTs that fire the bug. Statements are semicolon-terminated.
    reproducer = """
CREATE ROLE IF NOT EXISTS test WITH PASSWORD = 'test' AND LOGIN = true AND SUPERUSER = false;
GRANT SELECT PERMISSION ON KEYSPACE system TO test;
GRANT SELECT PERMISSION ON KEYSPACE system_schema TO test;
GRANT SELECT PERMISSION ON KEYSPACE system_views TO test;
GRANT SELECT PERMISSION ON KEYSPACE system_virtual_schema TO test;
LIST ALL PERMISSIONS OF test;
"""

    continuous_reproducer = True
    # No expected_output: this bug raises an exception (InvalidRequest code=2200), it does not persist or
    # return a wrong value. So expect_unready stays False and the mitigation oracle reads
    # NotReady = bug present, Ready = fixed. (See the module docstring for why continuous_reproducer is
    # still True despite the bare-cqlsh credential gap — the reproducer is build-dependent.)

    def post_deploy(self):
        """Enable the auth stack on the deployed cluster so the bug is reachable.

        This bug requires two cassandra.yaml startup settings that are NOT CQL- or runtime-settable:
          * authenticator: PasswordAuthenticator  -- GRANT is only supported on an authenticated,
            authz-enabled cluster (K8ssandra enables PasswordAuthenticator by default, but it is set
            explicitly here so the cluster comes up correctly under either operator default; in
            particular CassandraAuthorizer with AllowAllAuthenticator is a startup config-validation
            error, so PasswordAuthenticator must be set alongside the authorizer);
          * authorizer: CassandraAuthorizer       -- under the operator's default AllowAllAuthorizer,
            GRANT fails with "...not supported by AllowAllAuthorizer" (NOT the target signature). Only
            CassandraAuthorizer routes GRANT through DataResource.exists(), the keyspace-existence check
            that rejects the virtual keyspaces and trips this bug.

        We patch the operator-managed K8ssandraCluster CR's cassandraYaml block (a direct pod/StatefulSet
        edit would be reverted by the operator on the next reconcile) and let the operator perform a
        rolling restart. role_manager is left at its default (CassandraRoleManager). Mirrors
        auto_cassandra_16977.
        """
        cluster = self.app.cluster_name
        ns = self.app.namespace
        # JSON Patch (op:add) onto the dc1 datacenter's config.cassandraYaml. A --type=merge
        # patch on the datacenters[] array would wipe its required size/storageConfig (webhook
        # then rejects "datacenters[0].size: Required value"); a JSON Patch targets the exact
        # path and leaves array siblings intact (parent .../0/config exists from the CR
        # template's jvmOptions, so `add` creates cassandraYaml cleanly).
        patch = json.dumps(
            [
                {
                    "op": "add",
                    "path": "/spec/cassandra/datacenters/0/config/cassandraYaml",
                    "value": {
                        "authenticator": "PasswordAuthenticator",
                        "authorizer": "CassandraAuthorizer",
                    },
                }
            ]
        )
        logger.info(
            f"[AutoCassandra20171] Enabling auth stack on K8ssandraCluster '{cluster}' "
            f"(authenticator=PasswordAuthenticator, authorizer=CassandraAuthorizer)"
        )
        subprocess.run(
            f"kubectl patch k8ssandracluster {cluster} -n {ns} --type=json -p '{patch}'",
            shell=True,
            check=True,
            capture_output=True,
            text=True,
        )
        # Wait for the operator-driven rolling restart to bring the cluster back to Ready before the
        # reproducer runs CREATE ROLE / GRANT / LIST ALL PERMISSIONS.
        logger.info("[AutoCassandra20171] Waiting for cluster to be Ready after auth rollout")
        self.app._wait_for_cluster_ready(timeout=1800)
        logger.info("[AutoCassandra20171] Auth stack enabled; cluster Ready")
