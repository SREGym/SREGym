"""Mitigation oracle for the product-catalog missing-index fault.

The fault is fixed only when the index behind ``GetProduct`` is restored, which
flips the query plan from a Sequential Scan back to an Index Scan. Checking the
plan (rather than latency) makes grading deterministic and machine-independent,
and it rejects illegitimate "fixes" like scaling replicas, restarting pods, or
raising memory limits — those all leave the plan a Seq Scan because every
replica hits the same database.

The deployment check below is an anti-gaming guard (the agent must not "fix" the
fault by deleting or scaling away the service), *not* the fault signal. It
deliberately stops at "the deployment exists and is not scaled to zero" rather
than asserting pod readiness: product-catalog stays healthy both with and without
the index, so readiness carries no signal, and grading a run on it would just add
flakiness from unrelated restarts.
"""

import logging

from sregym.conductor.oracles.base import Oracle

logger = logging.getLogger(__name__)


class ProductCatalogMissingIndexMitigationOracle(Oracle):
    """Pass only when the GetProduct read uses an Index Scan again."""

    importance = 1.0

    def evaluate(self, *args, **kwargs) -> dict:
        print("== Mitigation Evaluation ==")
        results = {
            "success": False,
            "deployment_present": False,
            "uses_index_scan": False,
            "plan": None,
            "reason": "",
        }
        problem = self.problem

        # Guard against gaming by deleting/scaling the service away.
        try:
            results["deployment_present"] = problem.product_catalog_deployment_present()
        except Exception as exc:  # noqa: BLE001 - report, don't crash the stage
            results["reason"] = f"could not read the product-catalog deployment: {exc}"
            return results
        if not results["deployment_present"]:
            results["reason"] = "product-catalog deployment is missing or scaled to zero"
            return results

        # The real check: has the index behind GetProduct been restored?
        try:
            plan = problem.explain_getproduct_plan()
        except Exception as exc:  # noqa: BLE001
            results["reason"] = f"could not EXPLAIN the GetProduct query: {exc}"
            return results

        results["plan"] = plan.strip()
        results["uses_index_scan"] = problem.plan_uses_index_scan(plan)
        if not results["uses_index_scan"]:
            results["reason"] = "GetProduct still performs a Seq Scan on catalog.products; index not restored"
            print("Mitigation Result: Fail")
            return results

        results["success"] = True
        results["reason"] = "GetProduct uses an Index Scan on catalog.products; index restored"
        print("Mitigation Result: Pass")
        return results
