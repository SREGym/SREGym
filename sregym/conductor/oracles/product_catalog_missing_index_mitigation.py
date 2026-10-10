"""Mitigation oracle for the product-catalog missing-index fault.
"""

import logging

from sregym.conductor.oracles.base import Oracle

logger = logging.getLogger(__name__)


class ProductCatalogMissingIndexMitigationOracle(Oracle):
    """Pass only when the index behind GetProduct is back and the plan proves it."""

    importance = 1.0

    def evaluate(self, *args, **kwargs) -> dict:
        print("--- Mitigation Evaluation (product-catalog missing index) ---")
        results = {
            "success": False,
            "deployment_present": False,
            "index_exists": False,
            "uses_index_scan": False,
            "row_count": None,
            "plan": None,
            "reason": "",
        }
        problem = self.problem


        try:
            results["deployment_present"] = problem.product_catalog_deployment_present()
        except Exception as exc:  # noqa: BLE001 - report, don't crash the stage
            results["reason"] = f"could not read the product-catalog deployment: {exc}"
            return results
        if not results["deployment_present"]:
            results["reason"] = "product-catalog deployment is missing or scaled to zero"
            print("Mitigation Result: Fail")
            return results

        try:
            results["index_exists"] = problem.id_index_exists()
            results["row_count"] = problem.catalog_row_count()
            plan = problem.explain_getproduct_plan()
        except Exception as exc:  # noqa: BLE001
            results["reason"] = f"could not inspect catalog.products: {exc}"
            return results

        results["plan"] = plan.strip()
        results["uses_index_scan"] = problem.plan_uses_index_scan(plan)

        if not results["index_exists"]:
            results["reason"] = "no index covers catalog.products(id); GetProduct still sequentially scans the table"
            print("Mitigation Result: Fail")
            return results

        if results["row_count"] >= problem.PLAN_SIGNIFICANT_ROWS and not results["uses_index_scan"]:
            results["reason"] = (
                f"an index on catalog.products(id) exists, but GetProduct still plans a Seq Scan over "
                f"{results['row_count']} rows"
            )
            print("Mitigation Result: Fail")
            return results

        results["success"] = True
        results["reason"] = "the index on catalog.products(id) is restored and GetProduct no longer sequentially scans"
        print("Mitigation Result: Pass")
        return results
