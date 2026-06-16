"""CASSANDRA-19749: ALTER USER | ROLE IF EXISTS creates a phantom role if it does not exist.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-19749
Buggy: 4.1.5  ->  Fixed: 4.1.6

Reproduction summary (single auth-enabled node):
  On a node with the auth stack enabled (PasswordAuthenticator + CassandraAuthorizer +
  CassandraRoleManager), running `ALTER ROLE IF EXISTS <missing> WITH SUPERUSER = true;`
  (or the equivalent `ALTER USER IF EXISTS <missing> SUPERUSER;`) for a role that does NOT
  exist is silently accepted and, instead of being a no-op, PERSISTS a phantom superuser row
  in `system_auth.roles`. A subsequent `SELECT ... FROM system_auth.roles WHERE role='<missing>'`
  returns 1 row on the buggy build (is_superuser=True, no login, no password) but 0 rows on the
  fixed build. This is a privilege-escalation-class logic bug.

Verbatim buggy signature (from the reproduction evidence log; the phantom-role SELECT result on 4.1.5):
     repro19749_missing |      null |         True |      null |        null

Root cause (verified in the cassandra-4.1.5 source tree,
src/java/org/apache/cassandra/cql3/statements/AlterRoleStatement.java):
  validate() short-circuits with checkTrue(ifExists, ...) and returns WITHOUT throwing when the
  role is missing and IF EXISTS was given; execute() then unconditionally calls
  RoleManager.alterRole() whenever opts is non-empty, with no guard that the role actually
  exists. alterRole() upserts the row, creating the phantom superuser. The fix makes execute()
  a no-op when the role is absent under IF EXISTS.

NOTE on auth / reproducer plumbing (framework limitation, NOT a defect in this encoding):
  This bug only manifests with the auth stack enabled. The K8ssandra/cass-operator deployment
  used by GenericCustomBuildProblem already enables PasswordAuthenticator by default (it creates
  the `<cluster>-superuser` secret and starts Cassandra with skip_default_role_setup=true), so
  the auth precondition is satisfied automatically -- no cassandra.yaml edit or
  setup_preconditions hook is needed (and none of the framework's config-gating hooks could
  enable an authenticator anyway). However, the SHARED Cassandra reproducer plumbing in
  sregym/service/db_build_spec.py (`_cassandra_run_reproducer` and `_cassandra_reproducer_workload`)
  connects with bare `cqlsh <svc>` and does NOT pass `-u cassandra -p cassandra`; under the
  operator's enabled auth those connections are rejected with AuthenticationFailed. This is a
  framework-wide limitation affecting every auth-gated Cassandra problem, not specific to this
  file, and fixing it (passing credentials in the shared workload helpers) is out of scope here
  and cannot be statically verified. It is recorded loudly per the skill's "reproducer validation
  is narrow" gotcha. The bug itself is correctly encoded below as a single-node wrong-result repro.
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra19749(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.1.5"
    source_git_ref = "cassandra-4.1.5"
    # 4.1.5 already ships the bug (fix landed in 4.1.6), so deploy the stock image
    # instead of running a ~30-min `ant jar` source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/cql3/statements/AlterRoleStatement.java"
    root_cause_description = (
        "ALTER ROLE/USER IF EXISTS on a non-existent role persists a phantom superuser row in "
        "system_auth.roles instead of being a no-op. In AlterRoleStatement.java, validate() "
        "short-circuits with checkTrue(ifExists, ...) and returns without throwing when the role "
        "is missing and IF EXISTS was supplied, but execute() then unconditionally calls "
        "RoleManager.alterRole() whenever opts is non-empty, with no check that the role actually "
        "exists. alterRole() upserts the row, creating a superuser (is_superuser=True) with no "
        "login and no password. The fix makes execute() a no-op when the role does not exist "
        "under IF EXISTS."
    )

    # Single auth-enabled node, pure CQL. ALTER ROLE IF EXISTS <missing> is accepted as a no-op
    # by spec, but the buggy 4.1.5 build persists a phantom superuser; the trailing SELECT
    # surfaces that phantom row (1 row on buggy, 0 rows on fixed) so the mitigation probe can
    # grep for it. Statements are semicolon-terminated.
    reproducer = """
ALTER ROLE IF EXISTS repro19749_missing WITH SUPERUSER = true;
SELECT role, can_login, is_superuser FROM system_auth.roles WHERE role = 'repro19749_missing';
"""

    continuous_reproducer = True
    # Wrong-result bug: the probe greps cqlsh output for this BUGGY value (the phantom role name,
    # which only appears in the buggy build's 1-row SELECT result -- cqlsh does not echo input, so
    # the role name surfaces solely via the returned row). A robust substring is used rather than
    # the full pipe-delimited signature line, whose exact column spacing is fragile under grep -F.
    # expected_output present => expect_unready=True => Ready = bug present, NotReady = fixed.
    expected_output = "repro19749_missing"
