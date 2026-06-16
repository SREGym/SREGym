"""CASSANDRA-18371: nodetool listsnapshots omits snapshots whose tag contains a dot."""

from sregym.conductor.problems.cassandra_single_node_repro import CassandraSingleNodeProblem


class AutoCassandra18371(CassandraSingleNodeProblem):
    db_name = "cassandra"
    db_version = "4.1.1"
    cassandra_version = "4.1.1"
    source_git_ref = "cassandra-4.1.1"
    ring_namespace = "cassraw-18371"

    root_cause_file = "src/java/org/apache/cassandra/service/snapshot/SnapshotManager.java"
    root_cause_description = (
        "Snapshot directories may legally contain dots in their tag name. In 4.1.1 nodetool listsnapshots "
        "creates a dotted snapshot on disk but omits it from the listing, while an otherwise identical tag "
        "without a dot is listed."
    )
    bug_pattern = r"BUG_PRESENT: dotted snapshot omitted"

    def observe_bug(self) -> str:
        setup = """
        DROP KEYSPACE IF EXISTS repro18371;
        CREATE KEYSPACE repro18371 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
        CREATE TABLE repro18371.t (k int PRIMARY KEY, v int);
        INSERT INTO repro18371.t (k, v) VALUES (1, 1);
        """
        self.cql_file(setup, timeout=120)
        self.app.flush(self.pod, "repro18371")
        self.app.nodetool(self.pod, "snapshot -t nodot18371 repro18371")
        self.app.nodetool(self.pod, "snapshot -t dot.18371 repro18371")
        found = self.sh("find /var/lib/cassandra/data/repro18371 -type d -name 'dot.18371' -print | head")
        listing = self.app.nodetool(self.pod, "listsnapshots")
        marker = "BUG_PRESENT: dotted snapshot omitted\n" if "dot.18371" in found and "dot.18371" not in listing else ""
        return marker + "FOUND:\n" + found + "\nLIST:\n" + listing
