"""Behavioral oracle for catastrophic-regex-backtracking problems.

Passes iff the live product-reviews service can resolve a real product_id
through its DB path in under `_LATENCY_CEILING_S` seconds AND the returned
record set is non-empty. Fails on:

* ReDoS hang — the `timeout` kills the python subprocess and we see exit
  code 124 (GNU/BusyBox timeout) or a wall-clock overshoot.
* Agent stubs out the function to `return []` or similar — the length check
  catches that.
* Agent crashes the service — kubectl exec returns non-zero.
* Service not Ready — we short-circuit before probing.

The check is deliberately runtime-only. We don't look at the content of
/app/database.py at all: many different source-level fixes are valid, and
the only thing we care about is that the bug's observable symptom is gone.
"""

from __future__ import annotations

import shlex
import time

from sregym.conductor.oracles.base import Oracle

_PROBE_PY = (
    "import sys; "
    "from database import fetch_product_reviews_from_db; "
    "rows = fetch_product_reviews_from_db('OLJCESPC7Z'); "
    "sys.exit(0 if rows and len(rows) >= 1 else 91)"
)

# Upper bound on round-trip of one DB read. Pristine path runs in ~20 ms; we
# allow a full second for jitter / cold connection pool. A ReDoS patch
# wouldn't finish in 30 seconds let alone one.
_LATENCY_CEILING_S = 1.0

# Outer hard timeout used by the `timeout` binary inside the pod. Needs to be
# larger than _LATENCY_CEILING_S so slow-but-correct runs still exit cleanly
# and we distinguish "latency regression" from "hung". 5s is our kill line.
_PROBE_KILL_S = 5


class ReDoSBehavioralOracle(Oracle):
    def evaluate(self) -> dict:
        print("== Mitigation Evaluation (ReDoS behavioral) ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        deployment = self.problem.faulty_service  # 'product-reviews'

        results: dict = {"success": False}

        # Preflight: pods must be Running + Ready for any probe to be meaningful.
        ready_cmd = f"kubectl get deployment {deployment} -n {namespace} -o jsonpath={{.status.readyReplicas}}"
        ready_out = (kubectl.exec_command(ready_cmd) or "").strip()
        ready_replicas = int(ready_out) if ready_out.isdigit() else 0
        if ready_replicas < 1:
            print(f"❌ deployment/{deployment} has no ready replicas ({ready_out!r})")
            return results

        # Run the probe inside the pod via `timeout`. The service runs under a
        # venv at /venv/ (see the service's Dockerfile ENTRYPOINT), so we
        # invoke /venv/bin/python explicitly and `cd /app` so the
        # `from database import …` resolves the overlaid file whichever way
        # the agent has patched it.
        probe = (
            f"kubectl exec -n {namespace} deploy/{deployment} -- "
            f"sh -c {shlex.quote(f'cd /app && timeout {_PROBE_KILL_S} /venv/bin/python -c ' + shlex.quote(_PROBE_PY))}"
        )
        started = time.perf_counter()
        out = kubectl.exec_command(probe)
        elapsed = time.perf_counter() - started

        # kubectl.exec_command swallows non-zero exits and returns stderr as a
        # string. Our contract with the probe:
        #   exit 0  → DB path healthy, rows returned
        #   exit 91 → probe ran but result was empty (stub-style "fix")
        #   exit 124 → `timeout` killed the probe (ReDoS hang)
        #   any other non-zero → Python traceback / unexpected crash
        out_stripped = (out or "").strip()

        hung = elapsed >= _PROBE_KILL_S or "exit code 124" in out_stripped
        if hung:
            print(
                f"❌ probe exceeded timeout ({elapsed:.2f}s, output={out_stripped[:200]!r}) "
                "— DB call still catastrophically slow"
            )
            results["elapsed_s"] = elapsed
            return results

        if "exit code 91" in out_stripped:
            print("❌ probe returned empty result set — fix appears to stub the function instead of resolving the bug")
            results["elapsed_s"] = elapsed
            return results

        failed = "exit code" in out_stripped or "Traceback" in out_stripped or "Error" in out_stripped
        if failed:
            print(f"❌ probe exited non-zero: {out_stripped[:400]}")
            results["elapsed_s"] = elapsed
            return results

        if elapsed > _LATENCY_CEILING_S:
            print(f"❌ probe latency {elapsed:.2f}s exceeds ceiling {_LATENCY_CEILING_S}s — regression vs pristine")
            results["elapsed_s"] = elapsed
            return results

        print(f"✅ DB path healthy: probe completed in {elapsed * 1000:.0f} ms")
        results["success"] = True
        results["elapsed_s"] = elapsed
        return results


_PRODUCT_REVIEWS_PROBES = {
    "missing_fallback": """
import database as db, psycopg2, re, sys
if not re.search(r'statement_timeout\\s*=\\s*1(?:\\D|$)', db.db_connection_str):
    print('PROBE_OK'); sys.exit(0)
def unavailable(*args, **kwargs):
    raise psycopg2.OperationalError('probe outage')
db.psycopg2.connect = unavailable
try:
    result = db.fetch_product_reviews_from_db('OLJCESPC7Z')
except Exception:
    sys.exit(91)
if isinstance(result, list):
    print('PROBE_OK')
else:
    sys.exit(91)
""",
    "bounded_retry": """
import database as db, psycopg2
def unavailable(*args, **kwargs):
    raise psycopg2.OperationalError('probe outage')
db.psycopg2.connect = unavailable
try:
    db.fetch_product_reviews_from_db('OLJCESPC7Z')
except Exception:
    pass
print('PROBE_OK')
""",
    "bounded_fanout": """
import database as db, sys
connect = db.psycopg2.connect
calls = [0]
def counted(*args, **kwargs):
    calls[0] += 1
    if calls[0] > 2:
        raise RuntimeError('excess database fanout')
    return connect(*args, **kwargs)
db.psycopg2.connect = counted
rows = db.fetch_product_reviews_from_db('OLJCESPC7Z')
if rows and calls[0] <= 2:
    print('PROBE_OK')
else:
    sys.exit(91)
""",
}


class ProductReviewsFaultOracle(ReDoSBehavioralOracle):
    """Require a healthy DB read and exercise the variant's injected fault."""

    def __init__(self, problem, mode: str):
        super().__init__(problem)
        self.mode = mode

    def evaluate(self) -> dict:
        result = super().evaluate()
        if not result["success"]:
            return result

        probe = _PRODUCT_REVIEWS_PROBES[self.mode]
        command = (
            f"kubectl exec -n {self.problem.namespace} deploy/{self.problem.faulty_service} -- "
            f"sh -c {shlex.quote('cd /app && timeout 8 /venv/bin/python -c ' + shlex.quote(probe))}"
        )
        started = time.perf_counter()
        output = self.problem.kubectl.exec_command(command) or ""
        elapsed = time.perf_counter() - started
        result["fault_probe_elapsed_s"] = elapsed
        result["success"] = "PROBE_OK" in output and elapsed < 8
        print(f"{'✅' if result['success'] else '❌'} {self.mode} probe in {elapsed:.2f}s")
        return result
