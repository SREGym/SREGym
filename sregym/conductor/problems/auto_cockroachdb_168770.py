"""Auto-generated from https://github.com/cockroachdb/cockroach/issues/168770

Title: sql: identity columns incorrectly report a column default in pg_catalog and information_schema
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCockroachdb168770(GenericCustomBuildProblem):
    db_name   = "cockroachdb"
    issue_url = "https://github.com/cockroachdb/cockroach/issues/168770"
    root_cause_description = (
        "sql: identity columns incorrectly report a column default in pg_catalog and information_schema. **Describe the problem** CockroachDB exposes identity columns through `pg_catalog` and `information_schema` as if they had a regular column default (`DEFAULT nextval('<owned_seq>'::regclass)`). PostgreSQL 18 reports no default for identity columns — the identity property in `pg_attribute.attidentity` replaces the default mechanism, and the implicit owned sequence is materialized via the identity declaration rather than via a column default. The divergence shows up on three surfaces: - `pg_attribute.atthasdef` is `true` (PG: `false`) - `pg_attrdef` has a row for the identity column (PG: no row) - `information_schema.columns.column_default` is `nextval(...)` (PG: `NULL`) **To Reproduce** ```sql CREATE TABLE t (id INT GENERATED ALWAYS AS IDENTITY, x INT DEFAULT 7); SELECT a.attname, a.a"
    )
    reproducer = "CREATE TABLE t (id INT GENERATED ALWAYS AS IDENTITY, x INT DEFAULT 7);\n\nSELECT a.attname, a.attidentity, a.atthasdef,\n       d.adbin IS NOT NULL AS has_pg_attrdef\nFROM pg_attribute a\nLEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum\nWHERE a.attrelid = 't'::regclass AND a.attnum > 0\nORDER BY a.attnum;"
    continuous_reproducer = True
