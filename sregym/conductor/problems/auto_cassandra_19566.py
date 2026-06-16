"""JSON-encoded timestamp value does not always match non-JSON encoded value.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-19566
Fix commit: fba4a85b971a00e982361282cf6ea46f8ccf0cd1

Buggy: 4.1.4. Fixed: 4.1.5 (also 4.0.13, 5.0-rc1).

Reproduction (single-node, pure CQL):
  1. CREATE TABLE tbl (id int, ts timestamp, primary key (id)).
  2. INSERT a pre-Gregorian-cutover timestamp value: -13767019200000 (a date in year 1533).
  3. SELECT tounixtimestamp(ts), ts, tojson(ts) FROM tbl WHERE id=1.
  4. SELECT JSON * FROM tbl WHERE id=1.
  The same stored long renders as 1533-09-28 for the bare `ts` column (proleptic
  Gregorian, correct) but 1533-09-18 via tojson(ts) and SELECT JSON * (WRONG, 10 days
  off). The JSON serialization path used SimpleDateFormat (Julian/Gregorian-hybrid
  calendar) while the bare-column path used a proleptic-Gregorian formatter, so any
  pre-1582 timestamp diverges by the 10-day Julian/Gregorian offset.

Verbatim buggy signature (one row, the same stored value rendering as TWO dates):
   -13767019200000 | 1533-09-28 12:00:00.000000+0000 | "1533-09-18 12:00:00.000Z"
On the fixed image 4.1.5 all three renderings agree on 1533-09-28.
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra19566(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.1.4"
    source_git_ref = "cassandra-4.1.4"
    root_cause_file = "src/java/org/apache/cassandra/serializers/TimestampSerializer.java"
    root_cause_description = (
        "The JSON-encoded rendering of a timestamp does not match the bare-column rendering "
        "for dates before the 1582 Gregorian calendar reform. For the stored value "
        "-13767019200000, the bare `ts` column renders as 1533-09-28 (proleptic Gregorian, "
        "correct) while tojson(ts) and SELECT JSON * render the SAME stored long as 1533-09-18 "
        "(wrong, off by exactly the 10-day Julian/Gregorian offset). The root cause is in "
        "TimestampSerializer.java: the JSON serialization path formatted Date objects with "
        "SimpleDateFormat, which uses a Julian/Gregorian-hybrid calendar with a cutover in 1582, "
        "whereas the bare-column path uses a proleptic-Gregorian formatter. The fix converts the "
        "Date to an Instant and formats it with java.time.DateTimeFormatter (always proleptic "
        "Gregorian) so both paths agree. Fixed in 4.1.5."
    )
    reproducer = """
DROP KEYSPACE IF EXISTS repro19566;
CREATE KEYSPACE repro19566 WITH REPLICATION = {'class': 'SimpleStrategy', 'replication_factor': 1};
USE repro19566;
CREATE TABLE tbl (id int, ts timestamp, primary key (id));
INSERT INTO tbl (id, ts) VALUES (1, -13767019200000);
SELECT tounixtimestamp(ts), ts, tojson(ts) FROM tbl WHERE id=1;
SELECT JSON * FROM tbl WHERE id=1;
"""
    continuous_reproducer = True
    # Wrong-result bug: grep for the WRONG date emitted only by the buggy JSON path.
    # On 4.1.4 tojson(ts)/SELECT JSON * emit 1533-09-18; the fixed 4.1.5 emits 1533-09-28
    # everywhere, so 1533-09-18 is the unique discriminator (Ready = bug present).
    expected_output = "1533-09-18"
    # 4.1.4 already ships the bug (fix is 4.1.5), so deploy the stock image instead of an ant-jar build.
    prebuilt_from_stock = True
