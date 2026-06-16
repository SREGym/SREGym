"""CASSANDRA-16703: invalid custom query handler is ignored at startup."""

from sregym.conductor.problems.cassandra_single_node_repro import CassandraSingleNodeProblem


class AutoCassandra16703(CassandraSingleNodeProblem):
    db_name = "cassandra"
    db_version = "4.0.0"
    cassandra_version = "4.0.0"
    source_git_ref = "cassandra-4.0.0"
    ring_namespace = "cassraw-16703"
    jvm_extra_opts = "-Dcassandra.custom_query_handler_class=java.lang.String"

    root_cause_file = "src/java/org/apache/cassandra/service/ClientState.java"
    root_cause_description = (
        "An invalid custom_query_handler_class should be a fatal startup configuration error. In 4.0.0 "
        "ClientState logs that java.lang.String cannot be cast to QueryHandler but continues by silently "
        "defaulting to normal query handling, so a bad handler configuration is ignored."
    )
    bug_pattern = r"ignoring by defaulting on normal query handling"

    def observe_bug(self) -> str:
        return self.app.system_log(self.pod) + "\n" + self.cql("SHOW VERSION;", timeout=60)
