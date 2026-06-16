"""Adding new nodes to an auth-enabled cluster reverts the cassandra superuser password to default.

Title:   When adding new nodes to a cluster which has authentication enabled, we end up losing
         cassandra user's current credentials and they get reverted back to default cassandra/cassandra.
JIRA:    https://issues.apache.org/jira/browse/CASSANDRA-12525
Buggy:   4.1.0   ->   Fixed: 4.1.1 (also 3.0.29, 3.11.15, 4.0.8, 5.0)
Components: Cluster/Schema, Local/Config
Fix commit: 8ecd7616fe5d3ce0cfe8f4621eda1905a9110db1

Reproduction summary (single auth-enabled cluster, faithful per the evidence log):
  CassandraRoleManager.setupDefaultRole() calls hasExistingRoles(); when it returns false the node runs
  createDefaultRoleQuery() to (re)insert the default `cassandra` superuser. In 4.1.0 that INSERT carries
  NO explicit timestamp, so it gets the node's current wall-clock timestamp. If an operator has already
  run `ALTER ROLE cassandra WITH PASSWORD=...` (write timestamp T1), a later default-role INSERT
  (timestamp T2 > T1) WINS Last-Write-Wins reconciliation and overwrites the altered password with the
  default `cassandra/cassandra`. The fix pins the INSERT to `USING TIMESTAMP 0` so it can never beat a
  real ALTER. The original report hit this when a freshly-joining node ran setupDefaultRole AFTER the
  ALTER (hasExistingRoles() read empty before system_auth had replicated/been repaired); the join is just
  one way to make hasExistingRoles() read empty. This reproduction MANUALLY REPLAYS THE EXACT STATEMENT
  setupDefaultRole emits (the no-timestamp default-role INSERT) on the live cluster after an ALTER — it
  does NOT add a node — which the evidence log establishes is the faithful single-cluster reproduction.

Verbatim buggy signature (from the reproduction evidence log): after the buggy no-timestamp default-role
INSERT, a client using the previously-set password `'password'` is rejected while default
`cassandra/cassandra` succeeds:

    Connection error: ('Unable to connect to any servers', {'127.0.0.1:9042': AuthenticationFailed('Failed to authenticate to 127.0.0.1:9042: Error from server: code=0100 [Bad credentials] message="Provided username cassandra and/or password are incorrect"')})

    release_version
              4.1.0
    (1 rows)

(The `release_version 4.1.0` row is returned over a session authenticated with `cassandra/cassandra` —
i.e. the default credentials work again after the buggy write, on the buggy build.)

Reproduction shape: config-gated auth sequence driven by a custom inject_fault() (NOT a pure-CQL
continuous reproducer). The bug is in role/auth state and requires (a) PasswordAuthenticator enabled
(K8ssandra enables it by default) and (b) AUTHENTICATED CQL with the superuser credentials to ALTER the
role and INSERT into system_auth.roles. The framework's CQL-only reproducer/mitigation pod connects with
`cqlsh <svc>` and NO credentials (see _cassandra_reproducer_workload in db_build_spec.py), so it cannot
perform the authenticated write or probe the authenticated result at all. inject_fault() therefore drives
the full ALTER + no-timestamp INSERT + login-test sequence via kubectl-exec on the server pod, using the
K8ssandra-managed superuser credentials.

continuous_reproducer is left False (diagnosis-only, mitigation_oracle = None) for a decisive reason:
the manual no-timestamp INSERT is BUILD-INDEPENDENT. The A/B control in the evidence log (section 6) is
the QUERY FORM, not the build — issuing `INSERT ... VALUES(...)` with no timestamp reverts the password
on the FIXED 4.1.1 binary too, because Last-Write-Wins keys on the write's timestamp, not on which binary
issued it. Only setupDefaultRole() itself differs between builds (4.1.1 adds `USING TIMESTAMP 0`). So any
mitigation probe built on this manual reproducer would report "bug present" on the fixed build as well — a
permanent false NotReady — which makes a mitigation oracle invalid, not merely awkward. expected_output is
likewise left unset (it only feeds the mitigation-pod probe, which is not deployed here).
"""

import base64
import logging
import subprocess
import time

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

# Default-superuser identity and the bcrypt hash of the default password "cassandra", captured from the
# server in the evidence log. The hash is LOAD-BEARING: the buggy no-timestamp INSERT must write exactly
# this salted_hash or `cassandra/cassandra` would not actually log in and the symptom would not appear.
_DEFAULT_ROLE = "cassandra"
_DEFAULT_PASSWORD = "cassandra"
_ALTERED_PASSWORD = "password"
_DEFAULT_SALTED_HASH = "$2a$10$Mktd2LTSFAOh7GbIRaqv8uw9t/0HgnPr9MSTPktF7O9kObPm7wK/K"


class AutoCassandra12525(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.1.0"
    source_git_ref = "cassandra-4.1.0"
    # 4.1.0 already ships the bug (fix landed in 4.1.1), so deploy the stock image instead of running a
    # ~30-min `ant jar` source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/auth/CassandraRoleManager.java"
    root_cause_description = (
        "On an auth-enabled cluster, the cassandra superuser password silently reverts to the default "
        "'cassandra/cassandra' after a node runs CassandraRoleManager.setupDefaultRole(). setupDefaultRole() "
        "calls hasExistingRoles(); when it returns false the node runs createDefaultRoleQuery() to (re)insert "
        "the default 'cassandra' superuser into system_auth.roles. In 4.1.0 that INSERT carries NO explicit "
        "timestamp, so it is written with the node's current wall-clock timestamp. If an operator has already "
        "run ALTER ROLE cassandra WITH PASSWORD=... (timestamp T1), a later default-role INSERT (timestamp "
        "T2 > T1) wins Cassandra's Last-Write-Wins reconciliation and overwrites the altered password with the "
        "default one. The original report hit this when a freshly-joining node ran setupDefaultRole AFTER the "
        "ALTER while hasExistingRoles() still read empty (system_auth had not yet replicated/been repaired). "
        "The fix pins the default-role INSERT to USING TIMESTAMP 0 so it can never beat a real ALTER."
    )

    # Canonical buggy steps (documentation / human-readable record). The actual orchestration — the
    # operator ALTER, the no-timestamp default-role re-INSERT, and the login test — is driven by
    # inject_fault() below over AUTHENTICATED cqlsh, because (a) the writes require superuser credentials
    # and (b) the symptom is detected by login attempts, neither of which the CQL-only no-credential
    # reproducer pod can do.
    reproducer = """
-- Preconditions: cluster has authentication enabled (PasswordAuthenticator + CassandraAuthorizer);
-- the default `cassandra` superuser role exists (created by setupDefaultRole at bootstrap).
-- All statements below run via cqlsh AUTHENTICATED as the cluster superuser.

-- Step 1 (operator): set a non-default password. This write gets a real wall-clock timestamp T1.
ALTER ROLE cassandra WITH PASSWORD = 'password';
-- After this: login cassandra/password = OK; login cassandra/cassandra = FAIL.

-- Step 2 (BUGGY 4.1.0 form of createDefaultRoleQuery(), replayed verbatim): re-insert the default
-- superuser with NO explicit timestamp, so it is written with the current wall-clock timestamp T2 > T1.
-- '$2a$10$Mktd2LTSFAOh7GbIRaqv8uw9t/0HgnPr9MSTPktF7O9kObPm7wK/K' is the bcrypt hash of "cassandra".
INSERT INTO system_auth.roles (role, is_superuser, can_login, salted_hash)
  VALUES ('cassandra', true, true, '$2a$10$Mktd2LTSFAOh7GbIRaqv8uw9t/0HgnPr9MSTPktF7O9kObPm7wK/K');
-- BUGGY RESULT (T2 > T1 => default wins LWW): login cassandra/password = FAIL;
--   login cassandra/cassandra = OK; the credentials reverted to the default cassandra/cassandra.

-- The FIXED 4.1.1 form is identical but appends `USING TIMESTAMP 0`, which can never beat a real ALTER,
-- so on the fixed build the altered password is preserved. Note: only setupDefaultRole() differs between
-- builds; replaying the no-timestamp INSERT by hand reverts the password on the FIXED binary too, which
-- is why this is encoded diagnosis-only (no mitigation oracle).
"""
    # Diagnosis-only: see the module docstring. The manual no-timestamp INSERT is build-independent (LWW
    # keys on the write timestamp, not on which binary issued it), so a mitigation probe built on it would
    # be permanently false-NotReady on the fixed build. No expected_output for the same reason.
    continuous_reproducer = False

    # ── CQL driven over authenticated cqlsh on the server pod ─────────────────

    _ALTER_CQL = f"ALTER ROLE {_DEFAULT_ROLE} WITH PASSWORD = '{_ALTERED_PASSWORD}';"

    # The exact statement createDefaultRoleQuery() emits in 4.1.0, with NO `USING TIMESTAMP` clause.
    _BUGGY_DEFAULT_ROLE_INSERT = (
        "INSERT INTO system_auth.roles (role, is_superuser, can_login, salted_hash) "
        f"VALUES ('{_DEFAULT_ROLE}', true, true, '{_DEFAULT_SALTED_HASH}');"
    )

    _LOGIN_PROBE_CQL = "SELECT release_version FROM system.local;"

    def _server_pod(self) -> str | None:
        """Return the name of a Running cass-operator-managed Cassandra server pod.

        The cluster is deployed by the K8ssandra/cass-operator (see _cassandra_cluster_manifest in
        db_build_spec.py), which labels server pods with ``app.kubernetes.io/name=cassandra``. The
        reproduction is local to the auth state of one cluster, so any one running server pod suffices.
        """
        out = (
            subprocess.run(
                f"kubectl get pods -n {self.namespace} "
                f"-l app.kubernetes.io/name=cassandra "
                f"--field-selector=status.phase=Running "
                f"-o jsonpath='{{.items[0].metadata.name}}'",
                shell=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .strip("'")
        )
        return out or None

    def _superuser_creds(self) -> tuple[str, str]:
        """Read the K8ssandra-managed superuser credentials from the cluster secret.

        K8ssandra enables PasswordAuthenticator by default and generates a ``<cluster_name>-superuser``
        secret. These credentials are used to AUTHENTICATE the operator ALTER and the default-role
        re-INSERT below; the operations target the ``cassandra`` role (created by setupDefaultRole at
        bootstrap), which may be distinct from K8ssandra's generated superuser account. Fall back to
        cassandra/cassandra if the secret is absent.
        """
        secret = f"{self.app.cluster_name}-superuser"
        u = (
            subprocess.run(
                f"kubectl get secret {secret} -n {self.namespace} -o jsonpath='{{.data.username}}'",
                shell=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .strip("'")
        )
        p = (
            subprocess.run(
                f"kubectl get secret {secret} -n {self.namespace} -o jsonpath='{{.data.password}}'",
                shell=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .strip("'")
        )
        return (
            base64.b64decode(u).decode() if u else _DEFAULT_ROLE,
            base64.b64decode(p).decode() if p else _DEFAULT_PASSWORD,
        )

    def _exec_cql_as(
        self, pod: str, u_b64: str, p_b64: str, cql: str, timeout: int = 120
    ) -> subprocess.CompletedProcess:
        """Pipe CQL into cqlsh inside the ``cassandra`` container of ``pod``, authenticated with the
        base64-encoded username/password passed in."""
        return subprocess.run(
            f"kubectl exec -i -n {self.namespace} {pod} -c cassandra -- "
            f"bash -c '"
            f"U=$(echo {u_b64} | base64 -d); P=$(echo {p_b64} | base64 -d); "
            f'cqlsh -u "$U" -p "$P" --request-timeout=60'
            f"'",
            shell=True,
            capture_output=True,
            text=True,
            input=cql,
            timeout=timeout,
        )

    def _login_ok(self, pod: str, username: str, password: str) -> bool:
        """Return True if ``username``/``password`` can authenticate and run a trivial query.

        Used to demonstrate the symptom: after the buggy write, cassandra/cassandra (default) logs in
        while cassandra/password (the previously-altered password) is rejected with 'Bad credentials'.
        """
        u_b64 = base64.b64encode(username.encode()).decode()
        p_b64 = base64.b64encode(password.encode()).decode()
        try:
            result = subprocess.run(
                f"kubectl exec -i -n {self.namespace} {pod} -c cassandra -- "
                f"bash -c '"
                f"U=$(echo {u_b64} | base64 -d); P=$(echo {p_b64} | base64 -d); "
                f'cqlsh -u "$U" -p "$P" -e "{self._LOGIN_PROBE_CQL}"'
                f"'",
                shell=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            return result.returncode == 0 and "release_version" in result.stdout
        except subprocess.TimeoutExpired:
            return False

    @mark_fault_injected
    def inject_fault(self):
        """Drive the CASSANDRA-12525 password-reversion reproduction on the buggy 4.1.0 server pod.

        Steps (all via cqlsh authenticated as the cluster superuser, on the buggy 4.1.0 server pod):
          1. Ensure the buggy image is active (no-op when prebuilt_from_stock pre-deployed it).
          2. Operator: ALTER ROLE cassandra WITH PASSWORD='password' (real wall-clock timestamp T1).
             Confirm cassandra/password logs in and cassandra/cassandra is rejected.
          3. Replay the EXACT 4.1.0 createDefaultRoleQuery() INSERT with NO timestamp (so it is written
             with the current wall-clock timestamp T2 > T1) -> Last-Write-Wins makes the default
             cassandra/cassandra password overwrite the altered one.
          4. Login test (the behavioral symptom): cassandra/cassandra now logs in while cassandra/password
             is rejected with 'Bad credentials' — the credentials reverted to the default.
        """
        # 1. Make sure the buggy binary is the one running (lifecycle parity with the base class).
        if getattr(self, "_predeployed_buggy", False):
            logger.info("[AutoCassandra12525] Buggy image already deployed at cluster start — skipping swap")
        else:
            logger.info(f"[AutoCassandra12525] Swapping cluster to buggy image: {self._custom_image}")
            self.app.inject_buggy_image(self._custom_image)
            logger.info("[AutoCassandra12525] Buggy image active")

        self.setup_preconditions()

        pod = self._server_pod()
        if not pod:
            logger.warning("[AutoCassandra12525] No running Cassandra server pod found — cannot run reproducer")
            return
        logger.info(f"[AutoCassandra12525] Using server pod {pod}")

        try:
            username, password = self._superuser_creds()
            u_b64 = base64.b64encode(username.encode()).decode()
            p_b64 = base64.b64encode(password.encode()).decode()

            # 2. Operator sets a non-default password (write timestamp T1).
            logger.info("[AutoCassandra12525] ALTER ROLE cassandra WITH PASSWORD='password' (operator write T1)")
            self._exec_cql_as(pod, u_b64, p_b64, self._ALTER_CQL)
            # Let the role write and auth caches settle before exercising the bug.
            time.sleep(5)
            logger.info(
                "[AutoCassandra12525] After ALTER: "
                f"login cassandra/password={self._login_ok(pod, _DEFAULT_ROLE, _ALTERED_PASSWORD)}, "
                f"login cassandra/cassandra={self._login_ok(pod, _DEFAULT_ROLE, _DEFAULT_PASSWORD)} "
                "(expect True, False)"
            )

            # 3. Replay the buggy no-timestamp default-role INSERT (createDefaultRoleQuery() in 4.1.0).
            #    Its wall-clock timestamp T2 > T1, so LWW reverts the password to the default.
            logger.info(
                "[AutoCassandra12525] Replaying buggy createDefaultRoleQuery() INSERT (no USING TIMESTAMP) "
                "-> default password wins LWW"
            )
            self._exec_cql_as(pod, u_b64, p_b64, self._BUGGY_DEFAULT_ROLE_INSERT)
            time.sleep(5)

            # 4. Behavioral symptom: default creds work again; the altered password is rejected.
            default_ok = self._login_ok(pod, _DEFAULT_ROLE, _DEFAULT_PASSWORD)
            altered_ok = self._login_ok(pod, _DEFAULT_ROLE, _ALTERED_PASSWORD)
            logger.info(
                "[AutoCassandra12525] After buggy INSERT: "
                f"login cassandra/cassandra={default_ok}, login cassandra/password={altered_ok} "
                "(expect True, False)"
            )
            if default_ok and not altered_ok:
                logger.info(
                    "[AutoCassandra12525] Reproduced: credentials reverted to default cassandra/cassandra "
                    "(altered password 'password' rejected with 'Bad credentials')"
                )
            else:
                logger.warning(
                    "[AutoCassandra12525] Symptom not observed as expected "
                    f"(default_ok={default_ok}, altered_ok={altered_ok}); auth-cache timing or a "
                    "non-default superuser account may be involved"
                )
        except subprocess.TimeoutExpired:
            logger.warning("[AutoCassandra12525] cqlsh exec timed out while driving the reproducer")
        except Exception as e:  # tolerate exec hiccups; the role state mutation is the reproduction
            logger.warning(f"[AutoCassandra12525] inject_fault raised (continuing): {e}")
