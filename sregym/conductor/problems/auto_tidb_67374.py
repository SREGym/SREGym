"""Auto-generated from https://github.com/pingcap/tidb/issues/67374

Title: TiDB throws overflow error for CAST in WHERE clause depending on condition order, leading to inconsistent behavior
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoTidb67374(GenericCustomBuildProblem):
    db_name   = "tidb"
    issue_url = "https://github.com/pingcap/tidb/issues/67374"
    root_cause_description = (
        "TiDB throws overflow error for CAST in WHERE clause depending on condition order, leading to inconsistent behavior. ## Bug Report When a `CAST(t0.c0 AS DECIMAL)` expression is used in a `WHERE` clause together with other conditions, TiDB may either return an overflow error or successfully execute the query, depending on the order of the conditions. ### 1. Minimal reproduce step (Required) ```sql CREATE TABLE t0(c0 DOUBLE); INSERT INTO t0(c0) VALUES (1E81); -- \"INSERT INTO t0(c0) VALUES (1E80)\" would trigger the following ERROR message. SELECT * FROM t0 WHERE t0.c0 > 1 AND CAST(t0.c0 AS DECIMAL) AND t0.c0 <= 1; ERROR 1105 (HY000): [components/tidb_query_datatype/src/codec/mysql/decimal.rs:1916]: parsing 1000000000000000000000000000000000000000000000000000000000000000000000000000000000 will overflow SELECT * FROM t0 WHERE t0.c0 > 1 AND t0.c0 <= 1 AND CAST(t0.c0 AS DECIMAL); Empty set (0.00 sec) ``` ##"
    )
    reproducer = 'CREATE TABLE t0(c0 DOUBLE);\nINSERT INTO t0(c0) VALUES (1E81);\nSELECT * FROM t0 WHERE t0.c0 > 1 AND CAST(t0.c0 AS DECIMAL) AND t0.c0 <= 1;'
    continuous_reproducer = True
    expected_output = 'Empty set (0.00 sec)'
