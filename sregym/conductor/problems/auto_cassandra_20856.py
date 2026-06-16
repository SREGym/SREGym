"""CASSANDRA-20856: system_views.settings exposes TDE key-provider passwords."""

from sregym.conductor.problems.cassandra_single_node_repro import CassandraSingleNodeProblem


class AutoCassandra20856(CassandraSingleNodeProblem):
    db_name = "cassandra"
    db_version = "5.0.5"
    cassandra_version = "5.0.5"
    source_git_ref = "cassandra-5.0.5"
    ring_namespace = "cassraw-20856"

    root_cause_file = "src/java/org/apache/cassandra/db/virtual/SettingsTable.java"
    root_cause_description = (
        "SettingsTable should redact sensitive nested transparent_data_encryption_options key-provider "
        "parameters. In 5.0.5 the virtual table exposes keystore_password and key_password values; 5.0.6 "
        "redacts them as <REDACTED>."
    )
    bug_pattern = r"keystore_password=cassandra.*key_password=cassandra"

    def observe_bug(self) -> str:
        return self.cql(
            "SELECT name, value FROM system_views.settings "
            "WHERE name = 'transparent_data_encryption_options.key_provider.parameters';",
            timeout=120,
        )
