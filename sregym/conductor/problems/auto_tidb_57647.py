"""Auto-generated from https://github.com/pingcap/tidb/issues/57647

Title: Scalar function causes database crash.
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoTidb57647(GenericCustomBuildProblem):
    db_name   = "tidb"
    issue_url = "https://github.com/pingcap/tidb/issues/57647"
    root_cause_description = (
        "Scalar function causes database crash.. ## Bug Report Please answer these questions before submitting your issue. Thanks! ### 1. Minimal reproduce step (Required) <!-- a step by step guide for reproducing the bug. --> 1. schema. ```sql create table `t0` ( `vkey` integer, `pkey` integer, `c0` integer ); insert into `t0` values (1, 2, 3); ``` 2. sql statement. ```sql select * from `t0` where (nullif( 3 ^ 10 & (abs(-50)) , round(case when (((`t0`.`c0`) >= 1) or null) then 91 else 86 end) )) in (select `vkey` from `t0` where false); ``` ### 2. What did you expect to see? (Required) MySQL and TIDB have different execution results in the above case. The normal result in MySQL 8: ```SQL mysql> select * -> from `t0` -> where (nullif("
    )
    reproducer = 'create table `t0` (`vkey` integer, `pkey` integer, `c0` integer);\ninsert into `t0` values (1, 2, 3);\nselect * from `t0` where (nullif(3 ^ 10 & (abs(-50)), round(case when (((`t0`.`c0`) >= 1) or null) then 91 else 86 end))) in (select `vkey` from `t0` where false);'
    continuous_reproducer = True
    expected_output = 'Empty set'
