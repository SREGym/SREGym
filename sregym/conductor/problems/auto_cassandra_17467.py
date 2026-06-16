"""Confirmed deterministic: https://issues.apache.org/jira/browse/CASSANDRA-17467

Title: Timestamp issue with Cassandra 4.0.3 with Timezone value (CQL/Syntax).

Buggy: 4.0.3. Fixed: 4.0.4 (also 4.1-alpha1 / 4.1).

Reproduction (single-node, pure CQL):
  1. CREATE TABLE timetest(id int PRIMARY KEY, enddate timestamp, startdate timestamp).
  2. INSERT/SELECT using a CQL timestamp literal that has a SPACE before the timezone
     offset, e.g. '2022-03-20 12:48:56 +0530'.
  3. On 4.0.3 the CQL timestamp parser rejects it; the same literal with no space
     ('2022-03-20 12:48:56+0530') is accepted. This is a 4.0-line regression (3.11.x
     accepted both forms).

Verbatim buggy signature:
  InvalidRequest: Error from server: code=2200 [Invalid query]
  message="Unable to parse a date/time from '2022-03-20 12:48:56 +0530'"

Root cause: TimestampSerializer.dateStringToTimestamp builds its accepted formats by
combining the date/time patterns with offset patterns, but lacks a variant that allows a
space before an RFC822 numeric offset (+HHMM). So '2022-03-20 12:48:56 +0530' fails to
parse while '2022-03-20 12:48:56+0530' succeeds. Fixed in 4.0.4.
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra17467(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.0.3"
    source_git_ref = "cassandra-4.0.3"
    root_cause_file = "src/java/org/apache/cassandra/serializers/TimestampSerializer.java"
    root_cause_description = (
        "A CQL timestamp literal with a space before the timezone offset (e.g. "
        "'2022-03-20 12:48:56 +0530') is rejected on 4.0.3 with InvalidRequest code=2200 "
        "'Unable to parse a date/time from ...', while the same literal without the space "
        "('2022-03-20 12:48:56+0530') is accepted. The root cause is in "
        "TimestampSerializer.dateStringToTimestamp: the set of accepted date formats combines "
        "the date/time patterns with offset patterns but has no variant that allows a space "
        "before an RFC822 numeric offset (+HHMM), so the with-space literal fails to parse. "
        "This is a regression in the 4.0 line (3.11.x accepted both forms) and was fixed in 4.0.4."
    )
    reproducer = """
DROP KEYSPACE IF EXISTS repro17467;
CREATE KEYSPACE repro17467 WITH REPLICATION = {'class': 'SimpleStrategy', 'replication_factor': 1};
USE repro17467;
CREATE TABLE timetest (id int PRIMARY KEY, enddate timestamp, startdate timestamp);
INSERT INTO timetest (id, startdate, enddate) VALUES (1, '2022-03-20 12:48:56 +0530', '2022-03-20 12:48:56 +0530');
SELECT * FROM timetest WHERE id = 1 AND enddate = '2022-03-20 12:48:56 +0530' ALLOW FILTERING;
"""
    continuous_reproducer = True
    # 4.0.3 already ships the bug (fix is 4.0.4), so deploy the stock image instead of an ant-jar build.
    prebuilt_from_stock = True
