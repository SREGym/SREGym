"""CASSANDRA-20208: audit logging category lists are not trimmed in system_views.settings."""

from sregym.conductor.problems.cassandra_single_node_repro import CassandraSingleNodeProblem


class AutoCassandra20208(CassandraSingleNodeProblem):
    db_name = "cassandra"
    db_version = "5.0.2"
    cassandra_version = "5.0.2"
    source_git_ref = "cassandra-5.0.2"
    ring_namespace = "cassraw-20208"
    startup_prelude = r"""cat >>/etc/cassandra/cassandra.yaml <<'YAML'
audit_logging_options:
  enabled: true
  logger:
    - class_name: FileAuditLogger
  included_categories: DCL, ERROR, AUTH
YAML"""

    root_cause_file = "src/java/org/apache/cassandra/audit/AuditLogOptions.java"
    root_cause_description = (
        "Comma-separated audit logging category lists should be sanitized consistently. In 5.0.2 the "
        "system_views.settings value preserves spaces as 'DCL, ERROR, AUTH'; 5.0.3 normalizes it to "
        "'DCL,ERROR,AUTH'."
    )
    bug_pattern = r"audit_logging_options\.included_categories\s+\|\s+DCL, ERROR, AUTH"

    def observe_bug(self) -> str:
        return self.cql(
            "SELECT name, value FROM system_views.settings WHERE name = 'audit_logging_options.included_categories';",
            timeout=120,
        )
