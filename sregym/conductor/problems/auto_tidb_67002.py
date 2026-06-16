"""Auto-generated from https://github.com/pingcap/tidb/issues/67002

Title: TiDB Throws ERROR 1105 on DATEDIFF with CONCAT_WS and BIT Field
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoTidb67002(GenericCustomBuildProblem):
    db_name   = "tidb"
    issue_url = "https://github.com/pingcap/tidb/issues/67002"
    root_cause_description = (
        "TiDB Throws ERROR 1105 on DATEDIFF with CONCAT_WS and BIT Field. ## Bug Report ### 1. Minimal reproduce step (Required) ```sql DROP TABLE IF EXISTS table2; CREATE TABLE table2 (count INT, status BIT, timestamp DATETIME); INSERT INTO table2(count, status, timestamp) VALUES(100, 1, '2023-12-01 10:00:00'); SELECT DATEDIFF(CONCAT_WS(100, 100, tom12.status), tom12.timestamp) AS c11 FROM table2 AS tom12; --ERROR 1105 (HY000): expected integer SELECT DATEDIFF(CONCAT_WS(100, 100, 1), tom12.timestamp) AS c11 FROM table2 AS tom12; -- +------+ | c11 | +------+ | NULL | +------+ 1 row in set, 1 warning (0.00 sec) ``` ### 2. What did you expect to see? (Required) I expected both queries to return similar results, as tom12.status is 1 . ### 3. What did you see instead (Required) The first query throws an error: ERROR 1105 (HY000): expected integer. The seco"
    )
    reproducer = "DROP TABLE IF EXISTS table2;\nCREATE TABLE table2 (count INT, status BIT, timestamp DATETIME);\nINSERT INTO table2(count, status, timestamp) \nVALUES(100, 1, '2023-12-01 10:00:00');\nSELECT DATEDIFF(CONCAT_WS(100, 100, tom12.status), tom12.timestamp) AS c11 \nFROM table2 AS tom12;\n--ERROR 1105 (HY000): expected integer\nSELECT DATEDIFF(CONCAT_WS(100, 100, 1), tom12.timestamp) AS c11 \nFROM table2 AS tom12;\n--\n+------+\n| c11  |\n+------+\n| NULL |\n+------+\n1 row in set, 1 warning (0.00 sec)"
    continuous_reproducer = True
