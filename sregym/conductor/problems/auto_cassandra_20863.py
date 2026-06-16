"""CASSANDRA-20863: deprecated authorizer/role_manager settings disappear from system_views.settings."""

from sregym.conductor.problems.cassandra_single_node_repro import CassandraSingleNodeProblem


class AutoCassandra20863(CassandraSingleNodeProblem):
    db_name = "cassandra"
    db_version = "5.0.6"
    cassandra_version = "5.0.6"
    source_git_ref = "cassandra-5.0.6"
    ring_namespace = "cassraw-20863"

    root_cause_file = "src/java/org/apache/cassandra/db/virtual/SettingsTable.java"
    root_cause_description = (
        "The deprecated authorizer and role_manager aliases should remain visible in system_views.settings "
        "for compatibility. In 5.0.6 both settings return zero rows, while 5.0.7 returns AllowAllAuthorizer "
        "and CassandraRoleManager."
    )
    bug_pattern = r"BUG_PRESENT: authorizer and role_manager omitted"

    def observe_bug(self) -> str:
        authorizer = self.cql("SELECT name, value FROM system_views.settings WHERE name = 'authorizer';", timeout=120)
        role_manager = self.cql(
            "SELECT name, value FROM system_views.settings WHERE name = 'role_manager';", timeout=120
        )
        marker = ""
        if "(0 rows)" in authorizer and "(0 rows)" in role_manager:
            marker = "BUG_PRESENT: authorizer and role_manager omitted\n"
        return marker + authorizer + "\n" + role_manager
