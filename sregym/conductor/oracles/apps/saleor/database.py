"""Saleor: database."""

from __future__ import annotations

from sregym.conductor.oracles.postgres_lock_mitigation import PostgresLockMitigationOracle
from sregym.conductor.problems.apps.saleor.helpers import checkout_probe


class SaleorCatalogLockMitigationOracle(PostgresLockMitigationOracle):
    """``PostgresLockMitigationOracle`` with Saleor's storefront catalog query as the app-level check."""

    def _product_catalog_available(self) -> bool:
        result = checkout_probe(self.problem)
        if not result.get("ok"):
            print(f"❌ Storefront checkout fails at {result.get('step')}: {str(result.get('errors'))[:300]}")
        return bool(result.get("ok"))
