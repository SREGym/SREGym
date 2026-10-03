"""Every non-Lite SREGym problem ported to the Incident Arena apps (environment scaling).

The SREGym-Lite ports live in ``sregym/conductor/problems/lite_ia``; this package
holds the rest of the benchmark, written with the same helpers (``ported()``,
``with_app_health``). ``IA_PORTS`` maps each original registry id to its port;
variants of one fault on different original apps share a port. ``NOT_PORTED``
lists the problems with no faithful port and why.
"""

from sregym.conductor.problems.ia_ports import frappe, saleor, slack_a, slack_b

IA_PORTS: dict[str, tuple[str, type]] = {**slack_a.PORTS, **slack_b.PORTS, **saleor.PORTS, **frappe.PORTS}

IA_PORT_PROBLEMS = {problem_id: factory for problem_id, factory in IA_PORTS.values()}

NOT_PORTED = {
    "node_clock_drift_hotel_reservation": "CLOCK_REALTIME is host-kernel-global: in kind/DinD it would shift the host and every parallel environment (already excluded on emulated clusters).",
    "operator_overload_replicas": "TiDB-operator specific: none of the apps runs an operator or CRD.",
    "operator_non_existent_storage": "TiDB-operator specific: none of the apps runs an operator or CRD.",
    "operator_invalid_affinity_toleration": "TiDB-operator specific; without a controller the invalid spec is rejected by API validation.",
    "operator_security_context_fault": "TiDB-operator specific; without a controller the invalid spec is rejected by API validation.",
    "operator_wrong_update_strategy_fault": "TiDB-operator specific; without a controller the invalid spec is rejected by API validation.",
    "operator_wrong_operator_image": "TiDB-operator specific: the operator image fault has no operator to target.",
}

__all__ = ["IA_PORTS", "IA_PORT_PROBLEMS", "NOT_PORTED"]
