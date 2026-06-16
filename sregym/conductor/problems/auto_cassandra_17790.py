"""CASSANDRA-17790: config default for compaction throughput is 16 MiB/s instead of 64 MiB/s."""

from sregym.conductor.problems.cassandra_single_node_repro import CassandraSingleNodeProblem


class AutoCassandra17790(CassandraSingleNodeProblem):
    db_name = "cassandra"
    db_version = "4.0.5"
    cassandra_version = "4.0.5"
    source_git_ref = "cassandra-4.0.5"
    ring_namespace = "cassraw-17790"
    startup_prelude = "sed -ri '/^compaction_throughput_mb_per_sec:/d' /etc/cassandra/cassandra.yaml || true"

    root_cause_file = "src/java/org/apache/cassandra/config/Config.java"
    root_cause_description = (
        "When compaction_throughput_mb_per_sec is absent from cassandra.yaml, the Config class default in "
        "4.0.5 is still 16 MiB/s. The intended default is 64 MiB/s, which 4.0.6 reports through nodetool "
        "getcompactionthroughput."
    )
    bug_pattern = r"Current compaction throughput:\s+16 MB/s"

    def observe_bug(self) -> str:
        return self.app.nodetool(self.pod, "getcompactionthroughput")
