"""Auto-generated from https://github.com/pingcap/tidb/issues/67816

Title: A simple query can cause "ERROR 9005 (HY000): Region is unavailable"
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoTidb67816(GenericCustomBuildProblem):
    db_name   = "tidb"
    issue_url = "https://github.com/pingcap/tidb/issues/67816"
    root_cause_description = (
        "A simple query can cause \"ERROR 9005 (HY000): Region is unavailable\". ## Bug Report Hi, I found a crash in TiDB. Executing the following PoC causes `ERROR 9005 (HY000): Region is unavailable`. After this error occurs, any subsequent query on the affected tables will also fail with the same Region is unavailable error. ### 1. Minimal reproduce step (Required) ```sql CREATE TABLE t0(c0 DOUBLE, c1 INTEGER, c2 CHAR(1)); CREATE TABLE t1 LIKE t0; INSERT INTO t0 VALUES (0.3, 0, '᜝'); CREATE INDEX i0 ON t1(c2); SELECT * FROM t0 JOIN t1 ON t0.c0 < t1.c0 WHERE (CAST(CAST(t0.c2 AS CHAR) AS BINARY)) LIKE (CAST(false AS DATE)) AND t0.c0 >= t1.c0 HAVING CASE '1' WHEN CAST(t0.c1 AS BINARY) THEN -1 ELSE t1.c1 END; ERROR 9005 (HY000): Region is unavailable ``` ### 2. What did you expect to see? (Required) ```sql SELECT * FROM t0 JOIN t1 ON t0.c0 < t1.c0 WHERE (CAST(CAST("
    )
    reproducer = "CREATE TABLE t0(c0 DOUBLE, c1 INTEGER, c2 CHAR(1));\nCREATE TABLE t1 LIKE t0;\nINSERT INTO t0 VALUES (0.3, 0, '\u171d');\nCREATE INDEX i0 ON t1(c2);\nSELECT * FROM t0 JOIN t1 ON t0.c0 < t1.c0 WHERE (CAST(CAST(t0.c2 AS CHAR) AS BINARY)) LIKE (CAST(false AS DATE)) AND t0.c0 >= t1.c0 HAVING CASE '1' WHEN CAST(t0.c1 AS BINARY) THEN -1 ELSE t1.c1 END;"
    continuous_reproducer = True
