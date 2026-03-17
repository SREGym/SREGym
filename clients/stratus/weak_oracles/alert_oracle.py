import logging

from clients.stratus.weak_oracles.base_oracle import BaseOracle, OracleResult
from sregym.conductor.oracles.alert_oracle import AlertOracle as ConductorAlertOracle

logger = logging.getLogger("all.stratus.alert_oracle")


class _NamespaceHolder:
    """Duck-typed problem object that only exposes a namespace."""

    def __init__(self, namespace: str):
        self.namespace = namespace


class AlertOracle(BaseOracle):
    """Weak oracle that passes when no Prometheus alerts are firing in the namespace."""

    def __init__(self, namespace: str):
        self._oracle = ConductorAlertOracle(problem=_NamespaceHolder(namespace))

    def validate(self) -> OracleResult:
        result = self._oracle.evaluate()
        success = result.get("success", False)
        issues = [] if success else ["Prometheus alerts are still firing in the namespace."]
        return OracleResult(success=success, issues=issues)
