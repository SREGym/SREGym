"""Materialized views: incorrect quoting of a mixed-case UDF in the stored WHERE clause.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16836
Buggy: cassandra 3.11.11  ->  Fixed: cassandra 3.11.12 (also 4.1-alpha1 / 4.1).

Reproduction (single node, RF=1, UDFs enabled via cassandra.yaml):
  Create a table, a UDF whose name needs quoting because it is mixed-case ("Double"),
  and a materialized view whose WHERE clause references it: WHERE k < ks."Double"(2).
  The MV's WHERE clause is persisted/regenerated WITHOUT the quotes (as ks.Double(2)).
  The first INSERT into the base table triggers MV maintenance, which re-parses that
  stored WHERE clause; the unquoted Double is lowercased to ks.double, which does not
  exist, so the local mutation that maintains the MV fails.

Client surface (cqlsh):
  WriteFailure: Error from server: code=1500 [Replica(s) failed to execute write]
Server-side root exception (verbatim signature from system.log on cassandra:3.11.11):
  org.apache.cassandra.exceptions.InvalidRequestException: Unknown function repro16836.double called
"""

import json
import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem

logger = logging.getLogger(__name__)


class AutoCassandra16836(GenericCustomBuildProblem):
    db_name = "cassandra"
    # Buggy version = fix patch (3.11.12) - 1. The bug already ships in the stock
    # 3.11.11 image, so deploy/re-tag the stock image instead of an ~30-min ant build.
    db_version = "3.11.11"
    source_git_ref = "cassandra-3.11.11"
    prebuilt_from_stock = True

    # The log does not name a src/java path; this is the closest pointer the symptom
    # stack supports (View.getSelectStatement / View.getReadQuery rebuild the SELECT
    # from the MV's stored WHERE clause). The behavioral root cause below is what the
    # diagnosis oracle grades on.
    root_cause_file = "src/java/org/apache/cassandra/db/view/View.java"
    root_cause_description = (
        "A materialized view whose WHERE clause calls a mixed-case (quoted) user-defined "
        'function, e.g. WHERE k < ks."Double"(2), persists/regenerates that WHERE clause '
        "WITHOUT the quotes (as ks.Double(2)). When MV maintenance rebuilds the view's read "
        "query from the stored WHERE clause (View.getSelectStatement -> View.getReadQuery), "
        "the unquoted function name Double is lowercased to ks.double during re-parsing. That "
        "function does not exist, so FunctionCall$Raw.prepare raises InvalidRequestException: "
        "'Unknown function ks.double called', and the local mutation that maintains the MV "
        "fails (client sees WriteFailure code=1500). The fix preserves the original quoting of "
        "the UDF identifier when serializing the MV's WHERE clause."
    )

    # Single-node pure-CQL reproducer. The bug fires on the FIRST insert after MV
    # creation (no node restart needed — the documented restart trigger is sufficient
    # but not required; the stored WHERE clause is already unquoted at MV creation).
    #
    # DROP KEYSPACE IF EXISTS makes the block idempotent: the continuous-reproducer pod
    # loops this CQL, so each iteration must rebuild the keyspace/table/UDF/MV and then
    # re-trigger MV maintenance on the INSERT, otherwise a later iteration would fail on
    # "keyspace exists" rather than on the bug.
    reproducer = """
DROP KEYSPACE IF EXISTS repro16836;
CREATE KEYSPACE repro16836 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
USE repro16836;
CREATE TABLE t (k int PRIMARY KEY, v int);
CREATE FUNCTION "Double" (input int) CALLED ON NULL INPUT RETURNS int LANGUAGE java AS 'return input*2;';
CREATE MATERIALIZED VIEW mv AS SELECT * FROM t
   WHERE k < repro16836."Double"(2) AND k IS NOT NULL AND v IS NOT NULL
   PRIMARY KEY (v, k);
INSERT INTO t(k,v) VALUES (3,1);
"""
    continuous_reproducer = True
    # No expected_output: this bug raises an exception (WriteFailure / Unknown function),
    # it does not persist a wrong value. So expect_unready stays False and the mitigation
    # oracle reads NotReady = bug present, Ready = fixed.

    def post_deploy(self):
        """Enable user-defined functions on the deployed cluster.

        UDFs are disabled by default in Cassandra 3.11, so the reproducer's first DDL
        (CREATE FUNCTION) would be rejected on a stock cluster and the bug would never be
        reached. enable_user_defined_functions is a cassandra.yaml startup setting (not
        CQL- or runtime-settable in 3.11), so we patch the operator-managed K8ssandraCluster
        CR's cassandraYaml block and let the operator perform a rolling restart. Patching the
        pod/StatefulSet directly would be reverted by the operator on the next reconcile.
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
                    "value": {"enable_user_defined_functions": True},
                }
            ]
        )
        logger.info(
            f"[Cassandra16836] Enabling UDFs on K8ssandraCluster '{cluster}' "
            f"(cassandraYaml.enable_user_defined_functions=true)"
        )
        subprocess.run(
            f"kubectl patch k8ssandracluster {cluster} -n {ns} --type=json -p '{patch}'",
            shell=True,
            check=True,
            capture_output=True,
            text=True,
        )
        # Wait for the operator-driven rolling restart to bring the cluster back to Ready
        # before the reproducer runs CREATE FUNCTION.
        logger.info("[Cassandra16836] Waiting for cluster to be Ready after UDF rollout")
        self.app._wait_for_cluster_ready(timeout=1800)
        logger.info("[Cassandra16836] UDFs enabled; cluster Ready")
