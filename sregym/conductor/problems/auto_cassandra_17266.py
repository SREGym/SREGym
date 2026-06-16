"""CASSANDRA-17266: DESCRIBE MATERIALIZED VIEW emits invalid CQL (default_time_to_live = 0).

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-17266
Title: DESCRIBE KEYSPACE / MATERIALIZED VIEW generates invalid CQL for views.

Buggy: 4.0.3  ->  Fixed: 4.0.4 (also 4.1-alpha1 / 4.1). Components: CQL/Syntax.

Reproduction summary (single node, server-side DESCRIBE generation):
  1. Materialized views must be enabled first (`enable_materialized_views: true` in
     cassandra.yaml — off by default in 4.0.x). post_deploy() patches the K8ssandraCluster CR.
  2. CREATE TABLE with `default_time_to_live = 60`, then CREATE MATERIALIZED VIEW over it
     with NO TTL option (MVs forbid the option per CASSANDRA-12868).
  3. DESCRIBE MATERIALIZED VIEW wrongly emits `AND default_time_to_live = 0` inside the
     CREATE block. Replaying that DESCRIBE output to re-create the view is rejected by the
     server. The defect is in DESCRIBE output generation, NOT in the validation that rejects
     the option (the rejecting validation exists in both 4.0.3 and 4.0.4); the discriminator
     is the presence/absence of the `default_time_to_live` line in DESCRIBE output.

This is a WRONG-OUTPUT bug, so expected_output is set to the BUGGY value the buggy build
emits (`default_time_to_live = 0`). The reproducer-pod readiness probe greps DESCRIBE output
for it: Ready = bug present (line emitted on 4.0.3), Not Ready = fixed (line absent on 4.0.4).

Verbatim buggy signature (from the DESCRIBE MATERIALIZED VIEW output on 4.0.3):
    AND default_time_to_live = 0                <-- BUG: invalid for an MV
Corroboration: replaying the buggy DESCRIBE output fails with
    InvalidRequest: Error from server: code=2200 [Invalid query] message="Cannot set
    default_time_to_live for a materialized view. Data in a materialized view always expire
    at the same time than the corresponding data in the parent table."
"""

import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem

logger = logging.getLogger(__name__)


class AutoCassandra17266(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.0.3"
    source_git_ref = "cassandra-4.0.3"
    # 4.0.3 already ships the bug (fix landed in 4.0.4), so deploy the stock image
    # instead of running a ~30-min ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/schema/TableParams.java"
    root_cause_description = (
        "DESCRIBE MATERIALIZED VIEW generates invalid CQL: the emitted CREATE MATERIALIZED "
        "VIEW block wrongly includes 'AND default_time_to_live = 0', an option that "
        "materialized views forbid (CASSANDRA-12868). The defect is in DESCRIBE output "
        "generation (TableParams.appendCqlTo / TableMetadata.appendTableOptions), which "
        "unconditionally appends the default_time_to_live option without skipping it for "
        "view tables; replaying that DESCRIBE output to re-create the view is then rejected "
        "by the server. The fix omits the default_time_to_live line for materialized views."
    )

    # Idempotent CREATEs (IF NOT EXISTS) so the continuous reproducer loop can re-run the
    # block every iteration. The parent table carries default_time_to_live = 60; the MV is
    # created with NO TTL option. The final DESCRIBE MATERIALIZED VIEW is what exposes the
    # bug: on 4.0.3 its output contains the invalid 'default_time_to_live = 0' line.
    reproducer = """
CREATE KEYSPACE IF NOT EXISTS repro17266_ks WITH replication = {'class': 'SimpleStrategy', 'replication_factor': '1'};
USE repro17266_ks;
CREATE TABLE IF NOT EXISTS test_table (
  id text,
  date text,
  col1 text,
  col2 text,
  PRIMARY KEY (id, date)
) WITH default_time_to_live = 60 AND CLUSTERING ORDER BY (date DESC);
CREATE MATERIALIZED VIEW IF NOT EXISTS test_view AS
SELECT id, date, col1 FROM test_table
WHERE id IS NOT NULL AND date IS NOT NULL
PRIMARY KEY (id, date);
DESCRIBE MATERIALIZED VIEW repro17266_ks.test_view;
"""
    continuous_reproducer = True
    # WRONG-OUTPUT bug: the buggy 4.0.3 DESCRIBE output emits this line; 4.0.4 omits it.
    # The probe greps DESCRIBE output for this string => Ready = bug present, NotReady = fixed.
    expected_output = "default_time_to_live = 0"

    def post_deploy(self):
        """Enable materialized views on the deployed cluster.

        Materialized views are gated off by default in Cassandra 4.0.x
        ('Materialized views are disabled. Enable in cassandra.yaml to use.'), so the
        reproducer's CREATE MATERIALIZED VIEW would fail without this. Patch the
        K8ssandraCluster CR to add `enable_materialized_views: true` to cassandra.yaml,
        then wait for the operator to roll the datacenter back to Ready. This config
        gate is identical regardless of buggy/fixed build, so it does not affect the
        DESCRIBE-output discriminator.
        """
        cluster = self.app.cluster_name
        ns = self.namespace
        logger.info(f"[AutoCassandra17266] Enabling materialized views on {cluster} in {ns}")
        patch = '{"spec":{"cassandra":{"config":{"cassandraYaml":{"enable_materialized_views":true}}}}}'
        result = subprocess.run(
            f"kubectl patch k8ssandracluster {cluster} -n {ns} --type=merge -p '{patch}'",
            shell=True,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning(
                f"[AutoCassandra17266] enable_materialized_views patch failed: {result.stderr.strip()[:300]}"
            )
            return

        # The patch triggers a rolling restart of the datacenter; wait for it to
        # settle so the reproducer runs against a node that accepts MV DDL.
        subprocess.run(
            f"kubectl wait --for=condition=Ready k8ssandracluster/{cluster} -n {ns} --timeout=600s",
            shell=True,
            capture_output=True,
            text=True,
        )
        logger.info("[AutoCassandra17266] Materialized views enabled and cluster Ready")
