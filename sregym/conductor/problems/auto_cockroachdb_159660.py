"""Auto-generated from https://github.com/cockroachdb/cockroach/issues/159660

Title: sql: filter is wrongly omitted with LEFT LOOKUP JOIN
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCockroachdb159660(GenericCustomBuildProblem):
    db_name   = "cockroachdb"
    issue_url = "https://github.com/cockroachdb/cockroach/issues/159660"
    root_cause_description = (
        "sql: filter is wrongly omitted with LEFT LOOKUP JOIN. Schemas are 3 tables: ```sql CREATE TABLE t1 ( id UUID NOT NULL DEFAULT gen_random_uuid(), CONSTRAINT t1_pkey PRIMARY KEY (id ASC) ); CREATE TYPE mytyp AS ENUM ('enuma', 'enumb', 'enumc', 'enumd', 'enume', 'enumf'); CREATE TABLE t2 ( id UUID NOT NULL DEFAULT gen_random_uuid(), name VARCHAR(63) NOT NULL, org_id UUID NOT NULL, status mytyp NOT NULL DEFAULT 'enuma':::mytyp, CONSTRAINT t2_pkey PRIMARY KEY (id ASC), CONSTRAINT t2_org_id_fkey FOREIGN KEY (org_id) REFERENCES t1(id), INDEX t2_name_org_id_status_idx (name ASC, org_id ASC, status ASC) ); CREATE TYPE mytyp2 AS ENUM ('enumaa', 'enumbb', 'enumcc', 'enumdd'); CREATE TABLE t3 (t3_id UUID NOT NULL, tier INT2 NOT NULL DEFAULT 0:::INT8, measurement mytyp2 NOT NULL, CONSTRAINT t3_pkey PRIMARY KEY (t3_id ASC, measurement ASC, tier ASC"
    )
    reproducer = """\
CREATE TABLE t1 (
\tid UUID NOT NULL DEFAULT gen_random_uuid(),
\tCONSTRAINT t1_pkey PRIMARY KEY (id ASC)
);

CREATE TYPE mytyp AS ENUM ('enuma', 'enumb', 'enumc', 'enumd', 'enume', 'enumf');

CREATE TABLE t2
(
id UUID NOT NULL DEFAULT gen_random_uuid(),
name VARCHAR(63) NOT NULL,
org_id UUID NOT NULL,
status mytyp NOT NULL DEFAULT 'enuma':::mytyp,
CONSTRAINT t2_pkey PRIMARY KEY (id ASC),
CONSTRAINT t2_org_id_fkey FOREIGN KEY (org_id) REFERENCES t1(id),
INDEX t2_name_org_id_status_idx (name ASC, org_id ASC, status ASC)
);

CREATE TYPE mytyp2 AS ENUM ('enumaa', 'enumbb', 'enumcc', 'enumdd');

CREATE TABLE t3
(t3_id UUID NOT NULL, tier INT2 NOT NULL DEFAULT 0:::INT8, measurement mytyp2 NOT NULL,
\tCONSTRAINT t3_pkey PRIMARY KEY (t3_id ASC, measurement ASC, tier ASC),
\tCONSTRAINT t3_t3_id_fkey FOREIGN KEY (t3_id) REFERENCES t2(id)
);

INSERT INTO t1 DEFAULT VALUES;

INSERT INTO t2 (name, org_id, status)
SELECT 'testname', id, 'enumb' FROM t1 LIMIT 1;

INSERT INTO t3 (t3_id, tier, measurement)
SELECT id, 2, 'enumaa' FROM t2 LIMIT 1;

SELECT COUNT(*)
FROM t2
LEFT LOOKUP JOIN t3 ON t3.t3_id = t2.id
INNER JOIN t1 ON t1.id = t2.org_id
WHERE t3.tier IN (0, 1)
AND t3.measurement IS NOT NULL
AND t2.status = 'enumb';"""
    expected_output = "1"
    continuous_reproducer = True
