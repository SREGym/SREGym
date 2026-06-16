"""CASSANDRA-16902: A user should be able to view permissions of a role they created.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16902
Buggy: cassandra 4.0.1  ->  Fixed: cassandra 4.0.2 (also 3.0.26 / 3.11.12 / 4.1-alpha1 / 4.1).
Component: Feature/Authorization. (Sibling of CASSANDRA-16977, fixed in the same 4.0.2 release.)

Reproduction summary (single auth-enabled node, with PasswordAuthenticator + CassandraAuthorizer):
  When a role creates another role it should, by default, receive the DESCRIBE permission on the
  newly created role. In 4.0.1 that auto-grant-on-create is missing, so the (non-superuser)
  creating role cannot view the permissions of the role it just created. Concretely, as the
  superuser create a non-superuser `parent` with CREATE ON ALL ROLES, then in a SEPARATE
  authenticated session as `parent` create a `child` role and run LIST ALL PERMISSIONS OF 'child'.
  On the buggy 4.0.1 build that LIST is rejected with code=2100 [Unauthorized]; on the fixed 4.0.2
  build the same non-superuser `parent` can list the permissions of the `child` it created.

Verbatim buggy signature (from the reproduction evidence log, cassandra:4.0.1):
  <stdin>:1:Unauthorized: Error from server: code=2100 [Unauthorized] message="You are not authorized to view child's permissions"

NOTE on shape / encoding (read before changing this file):
  * CONFIG-GATED: the bug only manifests with CassandraAuthorizer enabled. With the operator's
    default AllowAllAuthorizer, LIST ALL PERMISSIONS is effectively a no-op and the missing
    default DESCRIBE grant is masked. post_deploy() therefore patches the operator-managed
    K8ssandraCluster CR's cassandraYaml block to set authenticator=PasswordAuthenticator and
    authorizer=CassandraAuthorizer (CassandraAuthorizer with AllowAllAuthenticator is a config
    error that blocks startup, so the authenticator must be set alongside the authorizer), then
    waits for the operator-driven rolling restart. A direct pod/StatefulSet edit would be reverted
    by the operator on the next reconcile, so the change must be at the CR level. Mirrors the
    sibling encoding in auto_cassandra_16977.py.
  * TWO AUTHENTICATED SESSIONS (inherent to this bug): the reproduction requires a NON-superuser
    `parent` to LIST permissions of a role it created — i.e. two distinct identities. The shared
    Cassandra reproducer plumbing in sregym/service/db_build_spec.py
    (`_cassandra_run_reproducer` / `_cassandra_reproducer_workload`) connects with a single bare,
    UNAUTHENTICATED `cqlsh <svc>` and runs one anonymous script; it can express neither the auth
    gating nor the superuser-then-parent session split. So inject_fault() is overridden to run the
    two sessions explicitly via `kubectl exec` (session A as the operator-generated superuser read
    from the `<cluster>-superuser` secret; session B as `parent`), following the kubectl-exec
    pattern in cassandra_20108.py / auto_cassandra_16977.py. The superuser is NOT cassandra/cassandra
    — the K8ssandra operator generates a random superuser and disables the default login — so the
    credentials are read from the secret rather than hardcoded. The `parent` password ('x') is
    hardcoded because this code creates the `parent` role with that password itself.
  * continuous_reproducer = False (diagnosis-only oracle): this is an auth-gated, two-session,
    error-producing bug with no expected_output. A looping continuous-reproducer pod uses the
    shared bare/unauthenticated single-script probe (no `-u/-p`, one identity), which under the
    enabled PasswordAuthenticator cannot even connect and cannot perform the two-session flow, so
    it would report NotReady forever and could never detect a fix. A perpetually-broken mitigation
    oracle is worse than none, so this problem is graded by the diagnosis oracle only (the same
    choice CassandraBugProblem makes). Recorded loudly per the skill's "reproducer validation is
    narrow" gotcha.
"""

import base64
import logging
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoCassandra16902(GenericCustomBuildProblem):
    db_name = "cassandra"
    # Buggy version = fix patch (4.0.2) - 1. The bug already ships in the stock 4.0.1 image, so
    # deploy/re-tag the stock image instead of an ~30-min `ant jar` source build.
    db_version = "4.0.1"
    source_git_ref = "cassandra-4.0.1"
    prebuilt_from_stock = True

    # The evidence log does not name a source file (only the mechanism: the missing default
    # DESCRIBE auto-grant to a role's creator at role-creation time). Best-effort area: role
    # management / role-creation lives in CassandraRoleManager. Path UNVERIFIED — see module
    # notes. The defect is the absence of the auto-grant, not a crash in a specific line.
    root_cause_file = "src/java/org/apache/cassandra/auth/CassandraRoleManager.java"
    root_cause_description = (
        "When a non-superuser role with CREATE ON ALL ROLES creates a new role, Cassandra should "
        "automatically grant the creating role the DESCRIBE permission on the newly created role so "
        "the creator can view (LIST) its permissions. In 4.0.1 this default grant-on-create is "
        "missing, so the creating role's `LIST ALL PERMISSIONS OF '<child>'` for the role it just "
        "created is rejected with code=2100 [Unauthorized] ('You are not authorized to view "
        "<child>'s permissions'). The fix restores the default DESCRIBE grant to a role's creator at "
        "creation time so the creator is authorized to view the created role's permissions."
    )

    # Documentation of the buggy two-session steps (executed by inject_fault() below, NOT fed to the
    # shared single-script reproducer plumbing, which cannot run two authenticated sessions).
    #
    # Session A (as the operator-generated superuser, read from the <cluster>-superuser secret):
    #   CREATE ROLE parent WITH PASSWORD='x' AND LOGIN=true;
    #   GRANT CREATE ON ALL ROLES TO parent;
    # Session B (as parent — the body's interactive `LOGIN parent` realized as a separate
    #            authenticated cqlsh session `-u parent -p x`):
    #   CREATE ROLE child WITH PASSWORD='x' AND LOGIN=true;
    #   LIST ALL PERMISSIONS OF 'child';   -- buggy 4.0.1: code=2100 [Unauthorized]
    reproducer = """
-- Session A (superuser):
CREATE ROLE parent WITH PASSWORD='x' AND LOGIN=true;
GRANT CREATE ON ALL ROLES TO parent;
-- Session B (as parent, separate authenticated cqlsh -u parent -p x):
CREATE ROLE child WITH PASSWORD='x' AND LOGIN=true;
LIST ALL PERMISSIONS OF 'child';
"""

    # See module docstring: diagnosis-only. Auth-gated + two-session + error (no expected_output),
    # so a looping bare/unauthenticated probe cannot grade mitigation.
    continuous_reproducer = False

    # ── Session A / B identities ──────────────────────────────────────────────
    _PARENT_USER = "parent"
    _PARENT_PASSWORD = "x"
    # Session A: create parent + grant CREATE ON ALL ROLES (run as the superuser).
    _SESSION_A_CQL = "CREATE ROLE parent WITH PASSWORD='x' AND LOGIN=true; GRANT CREATE ON ALL ROLES TO parent;"
    # Session B: create child + LIST ALL PERMISSIONS OF 'child' (run as parent). On the buggy
    # 4.0.1 build the LIST is rejected with code=2100 [Unauthorized].
    _SESSION_B_CQL = "CREATE ROLE child WITH PASSWORD='x' AND LOGIN=true; LIST ALL PERMISSIONS OF 'child';"

    def post_deploy(self):
        """Enable the auth stack on the deployed cluster so the bug is reachable.

        This bug requires two cassandra.yaml startup settings that are NOT CQL- or runtime-settable:
          * authenticator: PasswordAuthenticator  -- LIST PERMISSIONS needs authenticated sessions,
            and the two-session (superuser vs. parent) flow needs distinct logins;
          * authorizer:    CassandraAuthorizer     -- only this authorizer actually enforces the
            DESCRIBE permission on LIST ALL PERMISSIONS; the operator's default AllowAllAuthorizer
            makes LIST a no-op and masks the missing default grant.

        CassandraAuthorizer with AllowAllAuthenticator is a config-validation error that blocks
        startup, so PasswordAuthenticator must be set alongside the authorizer. We patch the
        operator-managed K8ssandraCluster CR's cassandraYaml block (a direct pod/StatefulSet edit
        would be reverted by the operator on the next reconcile) and let the operator perform a
        rolling restart. Mirrors auto_cassandra_16977.py (minus the UDF flag, which this bug does
        not need).
        """
        import json

        cluster = self.app.cluster_name
        ns = self.app.namespace
        # JSON Patch (op:add) onto the dc1 datacenter's config.cassandraYaml. A --type=merge
        # patch on the datacenters[] array would wipe its required size/storageConfig (webhook
        # then rejects "datacenters[0].size: Required value"); a JSON Patch targets the exact
        # path and leaves array siblings intact (parent .../0/config exists from the CR
        # template's jvmOptions, so `add` creates cassandraYaml cleanly).
        patch = json.dumps(
            [
                {
                    "op": "add",
                    "path": "/spec/cassandra/datacenters/0/config/cassandraYaml",
                    "value": {
                        "authenticator": "PasswordAuthenticator",
                        "authorizer": "CassandraAuthorizer",
                    },
                }
            ]
        )
        logger.info(
            f"[Cassandra16902] Enabling auth stack on K8ssandraCluster '{cluster}' "
            f"(authenticator=PasswordAuthenticator, authorizer=CassandraAuthorizer)"
        )
        subprocess.run(
            f"kubectl patch k8ssandracluster {cluster} -n {ns} --type=json -p '{patch}'",
            shell=True,
            check=True,
            capture_output=True,
            text=True,
        )
        # Wait for the operator-driven rolling restart to bring the cluster back to Ready before
        # inject_fault() runs the two authenticated sessions.
        logger.info("[Cassandra16902] Waiting for cluster to be Ready after auth rollout")
        self.app._wait_for_cluster_ready(timeout=1800)
        logger.info("[Cassandra16902] Auth stack enabled; cluster Ready")

    # ── Fault injection (custom: two authenticated sessions) ───────────────────

    def _superuser_credentials(self) -> tuple[str, str]:
        """Read the operator-generated superuser username/password from the
        `<cluster>-superuser` secret (base64-decoded). The K8ssandra operator
        generates a random superuser and disables the default cassandra/cassandra
        login, so these MUST be read from the secret, not hardcoded."""
        secret = f"{self.app.cluster_name}-superuser"
        ns = self.app.namespace
        username_b64 = subprocess.run(
            f"kubectl get secret {secret} -n {ns} -o jsonpath='{{.data.username}}'",
            shell=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        password_b64 = subprocess.run(
            f"kubectl get secret {secret} -n {ns} -o jsonpath='{{.data.password}}'",
            shell=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return (
            base64.b64decode(username_b64).decode(),
            base64.b64decode(password_b64).decode(),
        )

    def _cassandra_pod(self) -> str:
        """Name of a running Cassandra pod (label applied by the K8ssandra operator)."""
        return (
            subprocess.run(
                f"kubectl get pods -n {self.app.namespace} "
                f"-l app.kubernetes.io/name=cassandra "
                f"-o jsonpath='{{.items[0].metadata.name}}'",
                shell=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .strip("'")
        )

    def _exec_cql(self, pod: str, user: str, password: str, cql: str):
        """Run `cql` via cqlsh inside `pod` authenticated as `user`. Credentials are
        base64-encoded before being embedded in the shell command so special characters
        never break the shell quoting (same approach as CassandraApplication.run_cql)."""
        u_b64 = base64.b64encode(user.encode()).decode()
        p_b64 = base64.b64encode(password.encode()).decode()
        c_b64 = base64.b64encode(cql.encode()).decode()
        cmd = (
            f"kubectl exec -i -n {self.app.namespace} {pod} -c cassandra -- "
            f"bash -c '"
            f"U=$(echo {u_b64} | base64 -d); "
            f"P=$(echo {p_b64} | base64 -d); "
            f'echo {c_b64} | base64 -d | cqlsh -u "$U" -p "$P" --request-timeout=30'
            f"'"
        )
        return subprocess.run(cmd, shell=True, capture_output=True, text=True)

    @mark_fault_injected
    def inject_fault(self):
        """Swap to the buggy image (if not already deployed), then run the two-session reproducer.

        Does NOT call super().inject_fault(): the base implementation would fire the shared,
        unauthenticated, single-script Cassandra reproducer, which cannot perform the
        superuser-then-parent two-session flow this bug requires.
        """
        if self._predeployed_buggy:
            logger.info("[Cassandra16902] Buggy image already deployed at cluster start — skipping swap")
        else:
            logger.info(f"[Cassandra16902] Injecting fault: swapping to buggy image {self._custom_image}")
            self.app.inject_buggy_image(self._custom_image)
            logger.info("[Cassandra16902] Buggy image active")

        # Auth stack is enabled in post_deploy(); allow per-problem precondition hooks too.
        self.setup_preconditions()

        pod = self._cassandra_pod()
        if not pod:
            logger.warning("[Cassandra16902] No Cassandra pod found — cannot run reproducer")
            return

        su_user, su_password = self._superuser_credentials()

        # Session A (superuser): create parent + grant CREATE ON ALL ROLES.
        logger.info("[Cassandra16902] Session A (superuser): create parent + GRANT CREATE ON ALL ROLES")
        res_a = self._exec_cql(pod, su_user, su_password, self._SESSION_A_CQL)
        if res_a.returncode != 0:
            logger.info(f"[Cassandra16902] Session A cqlsh rc={res_a.returncode}: {res_a.stderr.strip()[:300]}")

        # Session B (as parent): create child + LIST ALL PERMISSIONS OF 'child'. On the buggy 4.0.1
        # build this LIST is rejected with code=2100 [Unauthorized] (the verbatim buggy signature).
        logger.info("[Cassandra16902] Session B (as parent): create child + LIST ALL PERMISSIONS OF 'child'")
        res_b = self._exec_cql(pod, self._PARENT_USER, self._PARENT_PASSWORD, self._SESSION_B_CQL)
        combined = f"{res_b.stdout}\n{res_b.stderr}".strip()
        if "Unauthorized" in combined or res_b.returncode != 0:
            logger.info(f"[Cassandra16902] Reproduced buggy signature (rc={res_b.returncode}): {combined[:300]}")
        else:
            logger.info(f"[Cassandra16902] Session B output (rc={res_b.returncode}): {combined[:300]}")
