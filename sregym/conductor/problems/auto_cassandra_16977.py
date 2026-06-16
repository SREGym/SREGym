"""CASSANDRA-16977: ArrayIndexOutOfBoundsException in FunctionResource#fromName.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16977
Buggy: cassandra 4.0.1  ->  Fixed: cassandra 4.0.2 (also 3.0.26 / 3.11.12 / 4.1-alpha1 / 4.1).
Component: Feature/Authorization.

Reproduction summary (single auth-enabled node, RF=1, with CassandraAuthorizer + UDFs):
  On a node with PasswordAuthenticator + CassandraAuthorizer and user-defined functions
  enabled, create a ZERO-ARG UDF, then GRANT EXECUTE ON FUNCTION ks.fn() TO a role. The
  GRANT itself SUCCEEDS -- it writes the authorization resource string `functions/ks/fn[]`.
  The bug fires on the READ path: `LIST ALL PERMISSIONS OF <role>` makes
  CassandraAuthorizer.listPermissionsForRole parse that stored resource back via
  Resources.fromName -> FunctionResource.fromName, which splits `fn[]` into a length-1
  array `["fn"]` and then indexes nameAndArgs[1], throwing ArrayIndexOutOfBoundsException.
  Client (cqlsh) sees NoHostAvailable; the server logs the AIOOBE below.

Verbatim buggy signature (from the reproduction evidence log, cassandra:4.0.1 system.log):
  java.lang.ArrayIndexOutOfBoundsException: Index 1 out of bounds for length 1
      at org.apache.cassandra.auth.FunctionResource.fromName(FunctionResource.java:190)
      at org.apache.cassandra.auth.Resources.fromName(Resources.java:60)
      at org.apache.cassandra.auth.CassandraAuthorizer.listPermissionsForRole(CassandraAuthorizer.java:282)
      at org.apache.cassandra.auth.CassandraAuthorizer.list(CassandraAuthorizer.java:262)
      at org.apache.cassandra.cql3.statements.ListPermissionsStatement.list(ListPermissionsStatement.java:112)
(The Jira description cites FunctionResource.java:178; the 4.0.1 build throws at line 190 --
same method, same code, only a line-number drift across branches.)

NOTE on auth / reproducer plumbing (framework limitation, NOT a defect in this encoding):
  This bug only manifests with CassandraAuthorizer enabled, which forces the auth stack on
  (PasswordAuthenticator). post_deploy() below sets authenticator/authorizer/UDF in the
  operator-managed cassandraYaml so the cluster comes up with the required configuration.
  However, the SHARED Cassandra reproducer plumbing in sregym/service/db_build_spec.py
  (`_cassandra_run_reproducer` and `_cassandra_reproducer_workload`) connects with a bare
  `cqlsh <svc>` and does NOT pass `-u cassandra -p cassandra`; under the enabled
  authenticator those connections are rejected with AuthenticationFailed. This is a
  framework-wide limitation affecting every auth-gated Cassandra problem (see also
  auto_cassandra_19749). It is inherent here because the bug REQUIRES CassandraAuthorizer
  (hence authentication), so the mitigation probe -- which also uses bare cqlsh -- cannot
  currently distinguish bug-present from fixed. Fixing the shared helpers to pass
  credentials is out of scope for this code-generation task and cannot be statically
  verified. Recorded loudly per the skill's "reproducer validation is narrow" gotcha. The
  bug itself is correctly encoded below as a single-node, auth+UDF-gated crash reproducer.
"""

import json
import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem

logger = logging.getLogger(__name__)


class AutoCassandra16977(GenericCustomBuildProblem):
    db_name = "cassandra"
    # Buggy version = fix patch (4.0.2) - 1. The bug already ships in the stock 4.0.1
    # image, so deploy/re-tag the stock image instead of an ~30-min `ant jar` source build.
    db_version = "4.0.1"
    source_git_ref = "cassandra-4.0.1"
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/auth/FunctionResource.java"
    root_cause_description = (
        "FunctionResource.fromName cannot parse the authorization resource for a zero-argument "
        "user-defined function. A zero-arg function's resource string is `functions/<ks>/<fn>[]`; "
        'FunctionResource.fromName does StringUtils.split(parts[2], "[|]") to separate the function '
        "name from its argument list, but split drops the empty trailing token, so `<fn>[]` yields a "
        'length-1 array ["<fn>"]. The code then accesses nameAndArgs[1] to read the argument types, '
        "throwing java.lang.ArrayIndexOutOfBoundsException: Index 1 out of bounds for length 1. The "
        "GRANT EXECUTE that stores the resource succeeds; the exception fires on the read path when "
        "CassandraAuthorizer.listPermissionsForRole parses the stored resource back during "
        "LIST PERMISSIONS (Resources.fromName -> FunctionResource.fromName). The fix handles the "
        "zero-arg case so the empty argument list parses to an empty arg-types list instead of "
        "indexing past the end of the array."
    )

    # Single auth-enabled node, pure CQL. The GRANT succeeds (writes functions/repro16977/zeroarg[]);
    # the trailing LIST ALL PERMISSIONS re-parses that resource and triggers the AIOOBE on the buggy
    # 4.0.1 build (cqlsh sees NoHostAvailable; the fixed 4.0.2 build returns the EXECUTE row cleanly).
    #
    # IF NOT EXISTS / CREATE OR REPLACE / re-issuing the (idempotent) GRANT keep the block replayable:
    # the continuous-reproducer pod loops this CQL, so every iteration must rebuild the keyspace /
    # function / role and re-grant before re-reaching the LIST that fires the bug. Statements are
    # semicolon-terminated.
    reproducer = """
CREATE KEYSPACE IF NOT EXISTS repro16977 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE OR REPLACE FUNCTION repro16977.zeroarg() CALLED ON NULL INPUT RETURNS bigint LANGUAGE java AS 'return System.currentTimeMillis();';
CREATE ROLE IF NOT EXISTS bob WITH PASSWORD = 'bob' AND LOGIN = true;
GRANT EXECUTE ON FUNCTION repro16977.zeroarg() TO bob;
LIST ALL PERMISSIONS OF bob;
"""

    continuous_reproducer = True
    # No expected_output: this bug raises an exception (ArrayIndexOutOfBoundsException, surfaced to
    # the client as NoHostAvailable), it does not persist or return a wrong value. So expect_unready
    # stays False and the mitigation oracle reads NotReady = bug present, Ready = fixed.

    def post_deploy(self):
        """Enable the auth stack + UDFs on the deployed cluster so the bug is reachable.

        This bug requires three cassandra.yaml startup settings that are NOT CQL- or
        runtime-settable:
          * authenticator: PasswordAuthenticator  -- LIST PERMISSIONS needs an authenticated session;
          * authorizer:    CassandraAuthorizer     -- only this authorizer routes through
            CassandraAuthorizer.listPermissionsForRole, which parses the stored resource and trips
            the AIOOBE (the operator's default AllowAllAuthorizer makes LIST PERMISSIONS a no-op and
            never reaches FunctionResource.fromName);
          * enable_user_defined_functions: true    -- UDFs are disabled by default, so CREATE FUNCTION
            (and thus the zero-arg function whose resource string trips the bug) would be rejected.

        All three are set explicitly (per the reproduction evidence log) so the cluster comes up with
        the required configuration under either operator default -- in particular, CassandraAuthorizer
        with AllowAllAuthenticator is a config-validation error that would prevent startup, so
        PasswordAuthenticator must be set alongside the authorizer. We patch the operator-managed
        K8ssandraCluster CR's cassandraYaml block (a direct pod/StatefulSet edit would be reverted by
        the operator on the next reconcile) and let the operator perform a rolling restart.
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
                        "enable_user_defined_functions": True,
                    },
                }
            ]
        )
        logger.info(
            f"[Cassandra16977] Enabling auth stack + UDFs on K8ssandraCluster '{cluster}' "
            f"(authenticator=PasswordAuthenticator, authorizer=CassandraAuthorizer, "
            f"enable_user_defined_functions=true)"
        )
        subprocess.run(
            f"kubectl patch k8ssandracluster {cluster} -n {ns} --type=json -p '{patch}'",
            shell=True,
            check=True,
            capture_output=True,
            text=True,
        )
        # Wait for the operator-driven rolling restart to bring the cluster back to Ready
        # before the reproducer runs CREATE FUNCTION / GRANT / LIST PERMISSIONS.
        logger.info("[Cassandra16977] Waiting for cluster to be Ready after auth/UDF rollout")
        self.app._wait_for_cluster_ready(timeout=1800)
        logger.info("[Cassandra16977] Auth stack + UDFs enabled; cluster Ready")
