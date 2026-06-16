"""CASSANDRA-16872: empty TOC component makes nodetool snapshot fail with a semaphore error."""

from sregym.conductor.problems.cassandra_single_node_repro import CassandraSingleNodeProblem


class AutoCassandra16872(CassandraSingleNodeProblem):
    db_name = "cassandra"
    db_version = "4.0.1"
    cassandra_version = "4.0.1"
    source_git_ref = "cassandra-4.0.1"
    ring_namespace = "cassraw-16872"

    root_cause_file = "src/java/org/apache/cassandra/io/sstable/format/SSTableReader.java"
    root_cause_description = (
        "Snapshotting a table whose SSTable TOC component exists but is empty should tolerate or report the "
        "corrupt component cleanly. In 4.0.1 the snapshot path requests zero snapshot permits and fails with "
        "'Requested permits (0) must be positive'; 4.0.2 handles the empty TOC without that semaphore failure."
    )
    bug_pattern = r"Requested permits \(0\) must be positive"

    def observe_bug(self) -> str:
        setup = """
        CREATE KEYSPACE IF NOT EXISTS repro WITH replication = {'class':'SimpleStrategy','replication_factor':1};
        CREATE TABLE IF NOT EXISTS repro.t (id int PRIMARY KEY, v text);
        INSERT INTO repro.t (id, v) VALUES (1, 'a');
        """
        out = ""
        for _ in range(3):
            out = self.cql_file(setup, timeout=120)
            if "NoHostAvailable" not in out:
                break
        out += self.app.flush(self.pod, "repro")
        out += self.app.nodetool(self.pod, "clearsnapshot -t emptytoc repro")
        script = r"""
TOC=$(find /var/lib/cassandra/data/repro -name '*-TOC.txt' | head -n1)
ls -l "$TOC"
: > "$TOC"
ls -l "$TOC"
"""
        out += self.sh(script, timeout=120)
        out += self.app.nodetool(self.pod, "snapshot -t emptytoc repro")
        return out
