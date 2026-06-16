"""CASSANDRA-16898: the clustering order logic in materialized view creation changed in 4.0.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16898
Buggy: cassandra 4.0.1  ->  Fixed: cassandra 4.0.2
Component: Feature/Materialized Views

Before 4.0, when a materialized view was created with no CLUSTERING ORDER specified, the
view's inherited clustering columns took the base table's clustering order. In 4.0 this
behaviour regressed: the clustering order was defaulted to ASC for all columns instead of
inheriting the base table order.

Reproduction (single node, RF=1, MVs enabled via cassandra.yaml):
  Create a base table whose clustering column ck is ordered DESC
  (WITH CLUSTERING ORDER BY (ck DESC)), then CREATE MATERIALIZED VIEW on it WITHOUT a
  WITH CLUSTERING ORDER clause, keeping ck as a clustering column (PRIMARY KEY (pk, v, ck)).
  On 4.0.1 the MV's inherited clustering column ck defaults to ASC instead of DESC.

This is a WRONG-RESULT / silent schema-correctness bug — CREATE succeeds (only a benign
"Materialized views are experimental" warning), but the persisted clustering order is wrong.

Verbatim buggy signature (DESCRIBE MATERIALIZED VIEW repro16898ks.mv on 4.0.1):
    WITH CLUSTERING ORDER BY (v ASC, ck ASC)
and system_schema.columns shows the inherited clustering column row: ck | clustering | 1 | asc
(the base table's ck is DESC, so ck should be DESC in the MV — the 4.0.2 control prints
"WITH CLUSTERING ORDER BY (v ASC, ck DESC)" / ck | clustering | 1 | desc). v stays ASC on both
builds because v was a regular column in the base table (no clustering order to inherit), so the
only column that flips is the inherited clustering column ck.
"""

import json
import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem

logger = logging.getLogger(__name__)


class AutoCassandra16898(GenericCustomBuildProblem):
    db_name = "cassandra"
    # Buggy version = fix patch (4.0.2) - 1. The bug already ships in the stock 4.0.1
    # image, so deploy/re-tag the stock image instead of an ~30-min ant build.
    db_version = "4.0.1"
    source_git_ref = "cassandra-4.0.1"
    prebuilt_from_stock = True

    # Verified against the cassandra-4.0.x source tree: CreateViewStatement.getType()
    # only applies the clustering-order (ReversedType) adjustment when the view explicitly
    # supplies a CLUSTERING ORDER (clusteringOrder.containsKey(name)); with no CLUSTERING
    # ORDER clause that branch is skipped and the inherited clustering column defaults to
    # ASC. The diagnosis oracle grades on root_cause_description, so that is the precise
    # behavioral statement; the file path below is the corresponding 4.0 schema-statement.
    root_cause_file = "src/java/org/apache/cassandra/cql3/statements/schema/CreateViewStatement.java"
    root_cause_description = (
        "Materialized view creation no longer inherits the base table's clustering order. "
        "When a CREATE MATERIALIZED VIEW omits WITH CLUSTERING ORDER, an inherited clustering "
        "column whose base-table order is DESC is persisted as ASC instead of DESC. In "
        "CreateViewStatement.getType(), the ReversedType (DESC) adjustment is applied ONLY when "
        "the view explicitly specifies a clustering order (clusteringOrder.containsKey(name)); "
        "with no CLUSTERING ORDER clause that branch is skipped, so the inherited column defaults "
        "to ASC rather than taking the base table's clustering order. The fix restores the pre-4.0 "
        "behaviour of inheriting the base table's clustering order when none is given."
    )

    # Single-node, pure CQL. This block is run both as the fault trigger and, in a loop, as
    # the mitigation readiness probe, so it is wrapped in DROP/CREATE KEYSPACE to be
    # self-contained and idempotent on every iteration: the second+ iterations DROP the
    # keyspace first, then rebuild the base table (ck DESC) and the MV (no CLUSTERING ORDER),
    # re-establishing the exact buggy schema state each run. The trailing
    # DESCRIBE MATERIALIZED VIEW is the discriminator the probe greps for "ck ASC".
    reproducer = """
DROP KEYSPACE IF EXISTS repro16898ks;
CREATE KEYSPACE repro16898ks WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};
CREATE TABLE repro16898ks.base (
  pk int, ck int, v int,
  PRIMARY KEY (pk, ck)
) WITH CLUSTERING ORDER BY (ck DESC);
CREATE MATERIALIZED VIEW repro16898ks.mv AS
  SELECT pk, ck, v FROM repro16898ks.base
  WHERE pk IS NOT NULL AND ck IS NOT NULL AND v IS NOT NULL
  PRIMARY KEY (pk, v, ck);
DESCRIBE MATERIALIZED VIEW repro16898ks.mv;
"""
    continuous_reproducer = True
    # Wrong-result bug: the BUGGY MV's clustering-order line is "WITH CLUSTERING ORDER BY
    # (v ASC, ck ASC)", whereas the fixed build prints "... ck DESC". The probe greps for
    # this buggy value, so Ready = bug still present, NotReady = fixed. "ck ASC" appears only
    # on the buggy build (fixed prints "ck DESC"); a bare "asc" would false-match because v
    # is ASC on both builds.
    expected_output = "ck ASC"

    def post_deploy(self):
        """Enable materialized views on the deployed cluster.

        Materialized views are disabled by default in Cassandra 4.0
        (cassandra.yaml: enable_materialized_views: false; renamed
        materialized_views_enabled in 4.1+), so the reproducer's CREATE MATERIALIZED VIEW
        would be rejected with "Materialized views are disabled. Enable in cassandra.yaml to
        use." on a stock cluster and the bug would never be reached. enable_materialized_views
        is a cassandra.yaml startup setting (not CQL- or runtime-settable in 4.0), so we patch
        the operator-managed K8ssandraCluster CR's cassandraYaml block and let the operator
        perform a rolling restart. Patching the pod/StatefulSet directly would be reverted by
        the operator on the next reconcile.
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
                    "value": {"enable_materialized_views": True},
                }
            ]
        )
        logger.info(
            f"[Cassandra16898] Enabling materialized views on K8ssandraCluster '{cluster}' "
            f"(cassandraYaml.enable_materialized_views=true)"
        )
        subprocess.run(
            f"kubectl patch k8ssandracluster {cluster} -n {ns} --type=json -p '{patch}'",
            shell=True,
            check=True,
            capture_output=True,
            text=True,
        )
        # Wait for the operator-driven rolling restart to bring the cluster back to Ready
        # before the reproducer runs CREATE MATERIALIZED VIEW.
        logger.info("[Cassandra16898] Waiting for cluster to be Ready after MV-enable rollout")
        self.app._wait_for_cluster_ready(timeout=1800)
        logger.info("[Cassandra16898] Materialized views enabled; cluster Ready")
