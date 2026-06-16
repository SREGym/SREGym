"""Auto-generated from https://github.com/pingcap/tidb/issues/67878

Title: Wrong Result: CAST(66 AND FALSE AS DECIMAL) IN (...) Returns Empty Set with RIGHT JOIN ... ON FALSE
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoTidb67878(GenericCustomBuildProblem):
    db_name   = "tidb"
    issue_url = "https://github.com/pingcap/tidb/issues/67878"
    root_cause_description = (
        "Wrong Result: CAST(66 AND FALSE AS DECIMAL) IN (...) Returns Empty Set with RIGHT JOIN ... ON FALSE. ## Bug Report Please answer these questions before submitting your issue. Thanks! ### 1. Minimal reproduce step (Required) -- create table DROP DATABASE IF EXISTS bug_tidb_case1; CREATE DATABASE bug_tidb_case1; USE bug_tidb_case1; DROP TABLE IF EXISTS t0; CREATE TABLE t0 ( c0 DECIMAL(10,0) UNSIGNED DEFAULT NULL, KEY i3 (c0) USING BTREE ) CHARSET=utf8mb4; INSERT INTO t0 VALUES (NULL),(0000000000),(0000000000),(0000000000),(0000000000), (0000000000),(0000000000),(1530369832),(2145182263); -- query SELECT `r`.`c1` AS `c1` FROM ( SELECT `l1`.`c0` AS `c0`, `l2`.`c0` AS `c2` FROM `t0` AS `l1` INNER JOIN `t0` AS `l2` ON FALSE ) AS `l` RIGHT JOIN (SELECT `c0` AS `c1` FROM `t0`) AS `r` ON (FALSE) WHERE CAST(66 AND FALSE AS DECIMAL) IN (`r`.`c1`, 65 OR COT(ABS(FALSE) + 1), -`l`.`c2`);"
    )
    reproducer = 'DROP DATABASE IF EXISTS bug_tidb_case1;\nCREATE DATABASE bug_tidb_case1;\nUSE bug_tidb_case1;\n\nDROP TABLE IF EXISTS t0;\nCREATE TABLE t0 (\n  c0 DECIMAL(10,0) UNSIGNED DEFAULT NULL,\n  KEY i3 (c0) USING BTREE\n) CHARSET=utf8mb4;\n\nINSERT INTO t0 VALUES\n(NULL),(0000000000),(0000000000),(0000000000),(0000000000),\n(0000000000),(0000000000),(1530369832),(2145182263);\n\nSELECT `r`.`c1` AS `c1`\nFROM (\n  SELECT `l1`.`c0` AS `c0`, `l2`.`c0` AS `c2`\n  FROM `t0` AS `l1` INNER JOIN `t0` AS `l2` ON FALSE\n) AS `l`\nRIGHT JOIN (SELECT `c0` AS `c1` FROM `t0`) AS `r` ON (FALSE)\nWHERE CAST(66 AND FALSE AS DECIMAL) IN (`r`.`c1`, 65 OR COT(ABS(FALSE) + 1), -`l`.`c2`);'
    continuous_reproducer = True
    expected_output = '0\n0\n0\n0\n0\n0'
