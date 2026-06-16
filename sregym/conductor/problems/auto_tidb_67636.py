"""Auto-generated from https://github.com/pingcap/tidb/issues/67636

Title: RIGHT JOIN + INNER JOIN returns incorrect rows due to join reorder
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoTidb67636(GenericCustomBuildProblem):
    db_name   = "tidb"
    issue_url = "https://github.com/pingcap/tidb/issues/67636"
    root_cause_description = (
        "RIGHT JOIN + INNER JOIN returns incorrect rows due to join reorder. ## Bug Report TiDB returns incorrect results when a `RIGHT JOIN` is followed by an `INNER JOIN`. The optimizer reorders the joins, placing the `INNER JOIN` before the `RIGHT JOIN` as the Build side. After the `RIGHT JOIN` pads `NULL` rows for unmatched right-side rows, TiDB does not re-check the `INNER JOIN` condition, causing `NULL` rows to pass through incorrectly. ### 1. Minimal reproduce step (Required) ```sql CREATE DATABASE test_repro; USE test_repro; CREATE TABLE t0 (t0_c0 int PRIMARY KEY NOT NULL, t0_c1 float); CREATE TABLE t3 (t3_c0 int PRIMARY KEY NOT NULL, t3_c3 float NOT NULL, t3_c4 float NOT NULL, INDEX idx_t3_c3(t3_c3)); CREATE TABLE t5 (t5_c0 smallint PRIMARY KEY NOT NULL, t5_c1 int NOT NULL, INDEX idx_t5_c1(t5_c1)); INSERT INTO t0 VALUES (1, 1.0), (2, 2.0); INSERT INT"
    )
    reproducer = 'CREATE DATABASE test_repro;\nUSE test_repro;\n\nCREATE TABLE t0 (t0_c0 int PRIMARY KEY NOT NULL, t0_c1 float);\nCREATE TABLE t3 (t3_c0 int PRIMARY KEY NOT NULL, t3_c3 float NOT NULL, t3_c4 float NOT NULL, INDEX idx_t3_c3(t3_c3));\nCREATE TABLE t5 (t5_c0 smallint PRIMARY KEY NOT NULL, t5_c1 int NOT NULL, INDEX idx_t5_c1(t5_c1));\n\nINSERT INTO t0 VALUES (1, 1.0), (2, 2.0);\nINSERT INTO t3 VALUES (1, 0.0, 999.0);\nINSERT INTO t5 VALUES (1, 100);\n\nANALYZE TABLE t0;\nANALYZE TABLE t3;\nANALYZE TABLE t5;\n\nSELECT COUNT(*) AS ref0\nFROM t3 RIGHT JOIN t0 ON t3.t3_c4 = t0.t0_c1\nINNER JOIN t5 ON t3.t3_c3 = t5.t5_c1;'
    continuous_reproducer = True
