"""Runtime behavioral oracles for postmortem-derived problems.

Each oracle issues a real probe against the live cluster and decides
pass/fail purely from observed behavior — never from the content of source
files or env vars. This catches stub-style "fixes" (e.g. agent makes the
function `return []` to avoid a regex hang) as well as the actual bug.

Each oracle needs only a handful of attributes from the bound Problem:

* `kubectl`, `namespace` — always.
* Probe-specific fields are listed in each oracle's docstring.

When `kubectl.exec_command` runs `kubectl exec ...` and the remote command
exits non-zero, the helper swallows the error and returns the kubectl
stderr text instead of stdout. That stderr is typically `command terminated
with exit code N` — we look for that string when checking exit codes.
"""

from __future__ import annotations

import shlex
import time

from sregym.conductor.oracles.base import Oracle


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _ready_replicas(kubectl, namespace: str, deployment: str) -> int:
    out = kubectl.exec_command(
        f"kubectl get deployment {deployment} -n {namespace} "
        "-o jsonpath={.status.readyReplicas}"
    )
    out = (out or "").strip()
    return int(out) if out.isdigit() else 0


def _all_pods_ok(kubectl, namespace: str) -> tuple[bool, str | None]:
    """Return (True, None) if every pod in the namespace is Running and Ready;
    otherwise (False, reason)."""
    pods = kubectl.list_pods(namespace)
    for pod in pods.items:
        if pod.status.phase != "Running":
            return False, f"pod {pod.metadata.name} phase={pod.status.phase}"
        for cs in pod.status.container_statuses or []:
            if cs.state.waiting and cs.state.waiting.reason:
                return False, f"container {cs.name} waiting={cs.state.waiting.reason}"
            if (
                cs.state.terminated
                and cs.state.terminated.reason
                and cs.state.terminated.reason != "Completed"
            ):
                return False, f"container {cs.name} terminated={cs.state.terminated.reason}"
            if not cs.ready:
                return False, f"container {cs.name} not ready"
    return True, None


def _kubectl_exec_with_timeout(
    kubectl,
    namespace: str,
    pod_or_deploy: str,
    inner_cmd: str,
    *,
    timeout_s: int,
) -> tuple[float, str]:
    """Run `kubectl exec` with an inner GNU `timeout` wrapper. Returns
    (elapsed_seconds, raw_output_or_stderr_string)."""
    full = (
        f"kubectl exec -n {namespace} {pod_or_deploy} -- "
        f"sh -c {shlex.quote(f'timeout {timeout_s} ' + inner_cmd)}"
    )
    started = time.perf_counter()
    out = kubectl.exec_command(full) or ""
    elapsed = time.perf_counter() - started
    return elapsed, out.strip()


# --------------------------------------------------------------------------- #
# Recommendation gRPC latency probe
# --------------------------------------------------------------------------- #


_RECOMMENDATION_PROBE_PY = (
    "import sys, grpc, demo_pb2, demo_pb2_grpc, os; "
    "ch = grpc.insecure_channel('localhost:' + os.environ['RECOMMENDATION_PORT']); "
    "stub = demo_pb2_grpc.RecommendationServiceStub(ch); "
    "req = demo_pb2.ListRecommendationsRequest("
    "    user_id='probe', product_ids=['OLJCESPC7Z'])\n"
    "try:\n"
    "    resp = stub.ListRecommendations(req, timeout=4)\n"
    "except Exception as e:\n"
    "    print('grpc-error:', repr(e)[:200]); sys.exit(80)\n"
    "ids = list(resp.product_ids)\n"
    "print('product_ids=', ids)\n"
    "sys.exit(0 if ids else 91)"
)


class RecommendationLatencyOracle(Oracle):
    """Pass iff a fresh ListRecommendations call returns a non-empty list
    within `latency_ceiling_s` and pods are healthy. Catches: hangs (any
    flavor of slow upstream propagation), empty-list stubs, RPC errors.

    Required problem attrs: `kubectl`, `namespace`. Optionally
    `latency_ceiling_s` (default 2.0); the probe kill timeout is 5s.
    """

    PROBE_KILL_S = 5
    DEFAULT_LATENCY_S = 2.0

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation (recommendation latency) ==")
        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        ceiling = float(getattr(self.problem, "latency_ceiling_s", self.DEFAULT_LATENCY_S))

        results: dict = {"success": False}
        if _ready_replicas(kubectl, namespace, "recommendation") < 1:
            print("❌ recommendation has no ready replicas")
            return results

        elapsed, out = _kubectl_exec_with_timeout(
            kubectl,
            namespace,
            "deploy/recommendation",
            f"/venv/bin/python -c {shlex.quote(_RECOMMENDATION_PROBE_PY)}",
            timeout_s=self.PROBE_KILL_S,
        )
        results["elapsed_s"] = elapsed

        if elapsed >= self.PROBE_KILL_S or "exit code 124" in out:
            print(f"❌ probe hung — elapsed {elapsed:.2f}s, output={out[:200]!r}")
            return results
        if "exit code 80" in out or "grpc-error" in out:
            print(f"❌ gRPC error from recommendation: {out[:300]}")
            return results
        if "exit code 91" in out:
            print("❌ probe got an empty product_ids list (stubbed fix)")
            return results
        if "exit code" in out or "Traceback" in out:
            print(f"❌ probe non-zero exit: {out[:300]}")
            return results
        if elapsed > ceiling:
            print(f"❌ latency {elapsed:.2f}s exceeded ceiling {ceiling:.2f}s")
            return results

        print(f"✅ recommendation gRPC healthy in {elapsed*1000:.0f} ms")
        results["success"] = True
        return results


# --------------------------------------------------------------------------- #
# Postgres role-auth probe
# --------------------------------------------------------------------------- #


class PostgresConnectOracle(Oracle):
    """Pass iff a fresh psql connection as a given role over TCP succeeds.

    Required problem attrs: `kubectl`, `namespace`,
    `pg_pod` (e.g. 'deploy/postgresql'),
    `pg_host` (the hostname services use, default 'postgresql'),
    `pg_db`, `pg_role`, `pg_password`. Optional `pg_port` (default 5432).
    """

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation (postgres role auth) ==")
        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        pod = self.problem.pg_pod
        host = getattr(self.problem, "pg_host", "postgresql")
        port = getattr(self.problem, "pg_port", 5432)
        db = self.problem.pg_db
        role = self.problem.pg_role
        password = self.problem.pg_password

        # Use TCP path explicitly with -h <host>; the unix-socket path would
        # bypass scram-sha-256 auth via pg_hba `local trust` and give a false
        # positive.
        cmd = (
            f"kubectl exec -n {namespace} {pod} -- "
            f"env PGPASSWORD={shlex.quote(password)} "
            f"psql -h {shlex.quote(host)} -p {int(port)} -U {shlex.quote(role)} "
            f"-d {shlex.quote(db)} -At -c 'SELECT 1'"
        )
        out = (kubectl.exec_command(cmd) or "").strip()
        ok = out.endswith("1")
        results = {"success": ok, "raw": out[:300]}
        if ok:
            print(f"✅ psql connect as {role} succeeded")
        else:
            print(f"❌ psql connect as {role} failed: {out[:300]}")
        return results


# --------------------------------------------------------------------------- #
# Productreviews INSERT probe (for integer-overflow et al)
# --------------------------------------------------------------------------- #


class ProductReviewsInsertOracle(Oracle):
    """Pass iff `INSERT INTO reviews.productreviews ... RETURNING id` succeeds.

    Catches int-overflow recurrence and column-type regressions.

    Required problem attrs: `kubectl`, `namespace`, `pg_pod`, `pg_superuser`,
    `pg_db`.
    """

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation (productreviews INSERT) ==")
        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        pod = self.problem.pg_pod
        sup = self.problem.pg_superuser
        db = self.problem.pg_db

        sql = (
            "INSERT INTO reviews.productreviews "
            "(product_id, username, description, score) "
            "VALUES ('PROBE', 'health_probe', 'oracle smoke', 5.0) RETURNING id"
        )
        cmd = (
            f"kubectl exec -n {namespace} {pod} -- "
            f"psql -U {shlex.quote(sup)} -d {shlex.quote(db)} -At -c {shlex.quote(sql)}"
        )
        out = (kubectl.exec_command(cmd) or "").strip()
        # psql -At with `RETURNING id` returns one numeric id line plus a
        # trailing "INSERT 0 1" status. Look for any line that's all digits.
        returned_id = None
        for line in out.splitlines():
            if line.strip().isdigit():
                returned_id = line.strip()
                break
        ok = returned_id is not None and int(returned_id) > 0
        results = {"success": ok, "raw": out[:300]}
        if ok:
            print(f"✅ INSERT succeeded, id={returned_id}")
        else:
            print(f"❌ INSERT failed: {out[:300]}")
        return results


# --------------------------------------------------------------------------- #
# Hostname-from-env DNS resolution probe
# --------------------------------------------------------------------------- #


_DNS_PROBE_PY = (
    "import socket, sys, os, re; "
    "raw = os.environ.get('TARGET_HOST', ''); "
    "host = raw.strip(); "
    "print('resolving:', repr(host))\n"
    "try:\n"
    "    ip = socket.gethostbyname(host)\n"
    "except Exception as e:\n"
    "    print('resolve-error:', repr(e)); sys.exit(80)\n"
    "print('ip:', ip)\n"
    "sys.exit(0)"
)


class AccountingHostResolvableOracle(Oracle):
    """Pass iff the hostname currently in a service's env var actually
    resolves in DNS. Handles both .NET `Host=X;User=...;` connection strings
    and simple `host:port` values. The probe is run from a Python-capable
    pod (product-reviews) so we don't need to install python in the target.

    Required problem attrs: `kubectl`, `namespace`, `faulty_service`,
    `env_var`.
    """

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation (DNS resolution from service env) ==")
        kubectl = self.problem.kubectl
        namespace = self.problem.namespace

        # Read the live env value off the deployment.
        out = (
            kubectl.exec_command(
                f"kubectl get deployment {self.problem.faulty_service} "
                f"-n {namespace} "
                "-o jsonpath="
                "'{.spec.template.spec.containers[0].env"
                "[?(@.name==\"" + self.problem.env_var + "\")].value}'"
            )
            or ""
        ).strip().strip("'")

        host = ""
        # Case 1: .NET-style "Host=foo;User=...;..."
        if "host=" in out.lower() and ";" in out:
            for part in out.split(";"):
                if part.strip().lower().startswith("host="):
                    host = part.split("=", 1)[1].strip()
                    break
        # Case 2: plain "host:port"
        elif ":" in out and not out.startswith("http"):
            host = out.rsplit(":", 1)[0].strip()
        # Case 3: "http://host:port" URL
        elif out.startswith("http"):
            rest = out.split("://", 1)[1]
            host = rest.split(":", 1)[0].split("/", 1)[0]
        # Case 4: bare hostname
        else:
            host = out.strip()

        if not host:
            print(f"❌ could not parse host from env value: {out[:200]!r}")
            return {"success": False, "raw": out}

        cmd = (
            f"kubectl exec -n {namespace} deploy/product-reviews -- "
            f"env TARGET_HOST={shlex.quote(host)} "
            f"/venv/bin/python -c {shlex.quote(_DNS_PROBE_PY)}"
        )
        probe_out = (kubectl.exec_command(cmd) or "").strip()
        ok = "ip:" in probe_out and "resolve-error" not in probe_out
        if ok:
            print(f"✅ host {host!r} resolves: {probe_out.splitlines()[-1]}")
        else:
            print(f"❌ host {host!r} did not resolve: {probe_out[:200]}")
        return {"success": ok, "raw": probe_out[:300]}


# --------------------------------------------------------------------------- #
# Deployment stability probe (Running + Ready + no recent restarts)
# --------------------------------------------------------------------------- #


class DeploymentStableOracle(Oracle):
    """Pass iff a named deployment has at least one Ready replica and no
    container has restarted in the last `restart_age_s` seconds.

    Required problem attrs: `kubectl`, `namespace`, `faulty_service`.
    Optional `restart_age_s` (default 30).
    """

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation (deployment stability) ==")
        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        deployment = self.problem.faulty_service
        max_age = int(getattr(self.problem, "restart_age_s", 30))

        if _ready_replicas(kubectl, namespace, deployment) < 1:
            print(f"❌ deployment {deployment} has no ready replicas")
            return {"success": False}

        # Look at the last restart time across all pods of the deployment.
        cmd = (
            f"kubectl get pods -n {namespace} -l app.kubernetes.io/component={deployment} "
            "-o jsonpath="
            "'{range .items[*]}{range .status.containerStatuses[*]}"
            "{.lastState.terminated.finishedAt}{\"\\n\"}{end}{end}'"
        )
        out = (kubectl.exec_command(cmd) or "").strip().strip("'")
        ok = True
        if out:
            from datetime import UTC, datetime
            now = datetime.now(UTC)
            for ts in out.splitlines():
                ts = ts.strip()
                if not ts:
                    continue
                try:
                    when = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=UTC
                    )
                except ValueError:
                    continue
                age = (now - when).total_seconds()
                if age < max_age:
                    print(
                        f"❌ container restart {age:.0f}s ago (within {max_age}s "
                        "instability window)"
                    )
                    ok = False
                    break
        if ok:
            print(f"✅ {deployment} stable; no restarts in last {max_age}s")
        return {"success": ok}


# --------------------------------------------------------------------------- #
# Cold-start latency probe (rollout restart → Ready)
# --------------------------------------------------------------------------- #


class RolloutLatencyOracle(Oracle):
    """Pass iff `kubectl rollout restart` of a named deployment becomes Ready
    within `cold_start_ceiling_s` wall-clock seconds. Catches expensive
    synchronous warm-ups injected by the latent-recovery problem.

    Required problem attrs: `kubectl`, `namespace`, `faulty_service`.
    Optional `cold_start_ceiling_s` (default 30).
    """

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation (cold-start latency) ==")
        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        deployment = self.problem.faulty_service
        ceiling = int(getattr(self.problem, "cold_start_ceiling_s", 30))

        kubectl.exec_command(
            f"kubectl rollout restart deployment/{deployment} -n {namespace}"
        )
        started = time.perf_counter()
        out = kubectl.exec_command(
            f"kubectl rollout status deployment/{deployment} -n {namespace} "
            f"--timeout={ceiling + 30}s"
        )
        elapsed = time.perf_counter() - started

        ok = "successfully rolled out" in (out or "") and elapsed <= ceiling
        results = {"success": ok, "elapsed_s": elapsed, "raw": (out or "")[:200]}
        if ok:
            print(f"✅ {deployment} cold-started in {elapsed:.1f}s")
        elif "successfully rolled out" not in (out or ""):
            print(f"❌ rollout never completed: {out[:200]}")
        else:
            print(f"❌ {deployment} took {elapsed:.1f}s (ceiling {ceiling}s)")
        return results
