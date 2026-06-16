"""Auto-generated from https://github.com/cockroachdb/cockroach/issues/147269

Title: "insert on conflict" with unused "returning into" variable in plpgsql causes row not to be inserted
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCockroachdb147269(GenericCustomBuildProblem):
    db_name   = "cockroachdb"
    issue_url = "https://github.com/cockroachdb/cockroach/issues/147269"
    root_cause_description = (
        "\"insert on conflict\" with unused \"returning into\" variable in plpgsql causes row not to be inserted. **Describe the problem** A \"insert ... on conflict ... returning ... into var\" statement in plpgsql fails to insert the row (despite there not being a conflict) when the variable is unused. **To Reproduce** ``` drop procedure if exists foo2(); drop procedure if exists foo1(); drop table if exists semaphore_waiting_line; create table semaphore_waiting_line ( semaphore_id string not null, user_id string not null, primary key (semaphore_id, user_id), entered timestamp not null default now() ); create or replace procedure foo1() language plpgsql as $$ declare v_inserted string; begin insert into semaphore_waiting_line (semaphore_id, user_id) values ('s1', 'u1') on conflict (semaphore_id, user_id) do nothing returning semaphore_id into v_inserted; raise notice '"
    )
    reproducer = "DROP TABLE IF EXISTS semaphore_waiting_line;\n\nCREATE TABLE semaphore_waiting_line (\n  semaphore_id STRING NOT NULL,\n  user_id STRING NOT NULL,\n  PRIMARY KEY (semaphore_id, user_id),\n  entered TIMESTAMP NOT NULL DEFAULT now()\n);\n\nCREATE OR REPLACE PROCEDURE foo2() LANGUAGE plpgsql AS $$\nDECLARE\n  v_inserted STRING;\nBEGIN\n  INSERT INTO semaphore_waiting_line\n    (semaphore_id, user_id) VALUES ('s1', 'u1')\n    ON CONFLICT (semaphore_id, user_id) DO NOTHING\n    RETURNING semaphore_id INTO v_inserted;\nEND $$;\n\nDELETE FROM semaphore_waiting_line;\nCALL foo2();\nSELECT count(*) FROM semaphore_waiting_line;"
    continuous_reproducer = True
    expected_output = '0'
