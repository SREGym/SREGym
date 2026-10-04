"""Every non-Lite SREGym problem ported to the Incident Arena apps (environment scaling).

The SREGym-Lite ports live in ``sregym/conductor/problems/lite_ia``; this package
holds the rest of the benchmark, written with the same helpers (``ported()``,
``with_app_health``). ``IA_PORTS`` maps each original registry id to its port;
variants of one fault on different original apps share a port. ``NOT_PORTED``
lists the problems with no faithful port and why.
"""

from sregym.conductor.problems.ia_ports import frappe, saleor, saleor_b, slack_a, slack_b, slack_c

IA_PORTS: dict[str, tuple[str, type]] = {
    **slack_a.PORTS,
    **slack_b.PORTS,
    **slack_c.PORTS,
    **saleor.PORTS,
    **saleor_b.PORTS,
    **frappe.PORTS,
}

IA_PORT_PROBLEMS = {problem_id: factory for problem_id, factory in IA_PORTS.values()}

NOT_PORTED = {
    **frappe.NOT_PORTED,
    "node_clock_drift_hotel_reservation": "CLOCK_REALTIME is host-kernel-global: in kind/DinD it would shift the host and every parallel environment (already excluded on emulated clusters).",
    "operator_overload_replicas": "TiDB-operator specific: none of the apps runs an operator or CRD.",
    "operator_non_existent_storage": "TiDB-operator specific: none of the apps runs an operator or CRD.",
    "operator_invalid_affinity_toleration": "TiDB-operator specific; without a controller the invalid spec is rejected by API validation.",
    "operator_security_context_fault": "TiDB-operator specific; without a controller the invalid spec is rejected by API validation.",
    "operator_wrong_update_strategy_fault": "TiDB-operator specific; without a controller the invalid spec is rejected by API validation.",
    "operator_wrong_operator_image": "TiDB-operator specific: the operator image fault has no operator to target.",
    "astronomy_shop_ad_service_failure": "flagd flag in the Astronomy ad service; none of the apps has an optional side service with an error toggle.",
    "astronomy_shop_ad_service_high_cpu": "flagd CPU-burn code path compiled into the ad service; no app exposes a runtime CPU-burn toggle.",
    "astronomy_shop_ad_service_manual_gc": "flagd forced full-JVM-GC code path; none of the apps is a JVM service with such a toggle.",
    "astronomy_shop_failed_readiness_probe": "flagd flag that makes the cart's gRPC health fail; no app has a toggle that fails its health endpoint (probe misconfiguration is covered by the readiness/liveness ports).",
    "astronomy_shop_payment_service_unreachable": "Saleor's checkout pays through the in-process dummy gateway: there is no network hop between checkout and payment to make unreachable (a payment App webhook to a blackhole added no errors on the user path).",
    "kafka_producer_leak": "Not completed. No broker in these apps fails like the original (Kafka heap OOM from a producer leak): RabbitMQ sizes its memory watermark from host RAM, so it is OOMKilled before its alarm, and Redpanda throttles instead of failing; work on a broker-flooding analogue was also halted by a safety check.",
    "gc_capacity_degradation": "No GC-bound service on a user path: GOGC=1 on Slack Spine's Go services (search-engine, kafkagate, dispatcher, geodns) changed CPU from ~2m to ~5m with no effect on users; the Python/TypeScript/Django services have no GOGC analogue.",
    "trainticket_f17_nested_sql_select_clause_error": "a malformed nested SELECT compiled into ts-voucher-service; a source-code bug with no analogue in the apps' images.",
}

__all__ = ["IA_PORTS", "IA_PORT_PROBLEMS", "NOT_PORTED"]
