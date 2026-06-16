"""Auto-generated from https://github.com/cockroachdb/cockroach/issues/133395

Title: sql: CockroachDB panics when executing SELECT statement with JOIN and ill-formed AS FOR SYSTEM TIME
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCockroachdb133395(GenericCustomBuildProblem):
    db_name   = "cockroachdb"
    issue_url = "https://github.com/cockroachdb/cockroach/issues/133395"
    root_cause_description = (
        "sql: CockroachDB panics when executing SELECT statement with JOIN and ill-formed AS FOR SYSTEM TIME. **Describe the problem** The latest version of the CockroachDB (v24.2.4 and the latest master commit: f15ee646b5111) crashes when executing the following query: ```sql CREATE TABLE v00 (c01 INT); SELECT ALL FROM ( v00 AS ta1401 NATURAL JOIN v00 AS ta1402 ) WITH ORDINALITY AS ta1403 AS OF SYSTEM TIME b'any_bytes' BETWEEN SYMMETRIC 'abc' AND 'abc'; ``` **To Reproduce** 1. In operating system Ubuntu 20.04 LTS, download the pre-build CockroachDB binaries (v24.2.4) from [link](https://www.cockroachlabs.com/docs/releases/#v24-2) 2. Run ./cockroach demo, and then paste the PoC query to the cockroach cli environment. 3. Observe the crash and log the stack information. **Expected behavior** The CockroachDB should return error from the statement as the AS FOR SYSTEM TIME is ill-"
    )
    reproducer = "CREATE TABLE v00 (c01 INT);\nSELECT ALL FROM ( v00 AS ta1401 NATURAL JOIN v00 AS ta1402 ) WITH ORDINALITY AS ta1403 AS OF SYSTEM TIME b'any_bytes' BETWEEN SYMMETRIC 'abc' AND 'abc';"
    continuous_reproducer = True
