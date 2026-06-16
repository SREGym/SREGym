"""Capital `P` confused for a Duration in the CQL parser where IDENT is expected.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-17919
Component: CQL / Syntax
Buggy: 4.1.1   Fixed: 3.11.15, 4.0.10, 4.1.2, 5.0-alpha1, 5.0

Reproduction (single node, pure CQL): issue a CQL statement that uses a bare
(unquoted) capital `P` where the grammar expects a plain identifier — e.g.
`CREATE TABLE P (k INT PRIMARY KEY)`. On the buggy 4.1.1 build the lexer mis-treats
`P` as the leading designator of an ISO-8601 Duration literal (P1Y2M...), so it
cannot be consumed as an identifier and the parse fails. Lowercase `p` is
unaffected, and the identical statement parses fine on fixed 4.1.2.

Verbatim buggy signature:
  SyntaxException: line 1:13 no viable alternative at input 'P' (CREATE TABLE [P]...)
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra17919(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.1.1"
    source_git_ref = "cassandra-4.1.1"
    root_cause_file = "src/antlr/Lexer.g"
    root_cause_description = (
        "A bare (unquoted) capital `P` cannot be used where the CQL grammar expects a plain "
        "identifier (IDENT), e.g. as a table name in `CREATE TABLE P (...)`. The ANTLR DURATION "
        "token rule in Lexer.g lists alternatives that begin with the literal `'P'` (the ISO-8601 "
        '"format with designators", P1Y2M3DT...), so the lexer greedily mis-lexes a leading capital '
        "`P` as the start of a Duration literal instead of an identifier, yielding "
        "`SyntaxException: line 1:13 no viable alternative at input 'P'`. Lowercase `p` is unaffected. "
        "The fix makes the parser accept capital `P` as an identifier in IDENT positions."
    )
    # Single-node pure-CQL reproducer. Lead with DROP KEYSPACE so the looping
    # continuous reproducer is idempotent: on a FIXED build, `CREATE TABLE P`
    # succeeds and the next iteration would otherwise error "table already exists";
    # dropping the keyspace each pass wipes table P so the loop's only failure is
    # the bug itself. Keyspace name is lowercase so the setup statements do not trip
    # the same lexer defect. `CREATE TABLE P (k INT PRIMARY KEY)` is kept verbatim
    # and last so the block's failure == the buggy SyntaxException (line 1:13).
    reproducer = """
DROP KEYSPACE IF EXISTS repro17919;
CREATE KEYSPACE repro17919 WITH REPLICATION = {'class': 'SimpleStrategy', 'replication_factor': 1};
USE repro17919;
CREATE TABLE P (k INT PRIMARY KEY);
"""
    continuous_reproducer = True
    # 4.1.1 already ships the bug (fixed in 4.1.2), so deploy the stock image
    # instead of running a ~30-min `ant jar` source build.
    prebuilt_from_stock = True
