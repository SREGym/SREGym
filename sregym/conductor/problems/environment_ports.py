"""Port registrations and source identities, separate from named suites."""

from sregym.conductor.problems.apps.frappe.ports import SOURCE_PORTS as FRAPPE_PORTS
from sregym.conductor.problems.apps.saleor.ports import SOURCE_PORTS as SALEOR_PORTS
from sregym.conductor.problems.apps.slack_spine.ports import SOURCE_PORTS as SLACK_SPINE_PORTS

SOURCE_PORTS = {**FRAPPE_PORTS, **SALEOR_PORTS, **SLACK_SPINE_PORTS}
_VARIANT_OF = {
    "duplicate_pvc_mounts_astronomy_shop": "duplicate_pvc_mounts_social_network",
    "duplicate_pvc_mounts_hotel_reservation": "duplicate_pvc_mounts_social_network",
    "readiness_probe_misconfiguration_astronomy_shop": "readiness_probe_misconfiguration_social_network",
    "readiness_probe_misconfiguration_hotel_reservation": "readiness_probe_misconfiguration_social_network",
    "rolling_update_misconfigured_hotel_reservation": "rolling_update_misconfigured_social_network",
    "service_dns_resolution_failure_astronomy_shop": "service_dns_resolution_failure_social_network",
    "wrong_dns_policy_hotel_reservation": "wrong_dns_policy_astronomy_shop",
    "wrong_dns_policy_social_network": "wrong_dns_policy_astronomy_shop",
    "wrong_service_selector_astronomy_shop": "wrong_service_selector_social_network",
    "wrong_service_selector_hotel_reservation": "wrong_service_selector_social_network",
}
SOURCE_PORTS.update({variant: SOURCE_PORTS[source] for variant, source in _VARIANT_OF.items()})
PORTED_PROBLEMS = {problem_id: factory for problem_id, factory in SOURCE_PORTS.values()}
LITE_IA_PORTS = {
    source: SOURCE_PORTS[source]
    for source in [
        "cronjob_sidecar_blocks_completion_hotel_reservation",
        "edge_request_filter_cpu_saturation",
        "network_policy_block",
        "env_variable_shadowing_astronomy_shop",
        "finalizer_deadlock_controller_hotel_reservation",
        "service_dns_resolution_failure_social_network",
        "service_wrong_pod_selection_hotel_reservation",
        "unschedulable_incorrect_port_assignment",
        "readiness_probe_misconfiguration_social_network",
        "duplicate_pvc_mounts_social_network",
        "admission_webhook_outage_hotel_reservation",
        "wrong_dns_policy_astronomy_shop",
        "wrong_service_selector_social_network",
        "rolling_update_misconfigured_social_network",
        "kafka_poison_pill_hol_block",
        "internal_traffic_policy_local_astronomy_shop",
        "mutating_webhook_resource_limits_social_network",
        "namespace_memory_limit",
        "secret_rotation_stale_env_credentials_astronomy_shop",
        "search_rate_retry_collapse_hotel_reservation",
        "valkey_auth_disruption",
    ]
}
NOT_PORTED = {
    "misconfig_app_hotel_res": "The fault is a purpose-built application image whose baked-in configuration points at the wrong database port (ghcr.io/sregym/hotel-reservation misconfig tag). Frappe reads its database host/port from the site config on the shared `erp` volume, not from the image, so the same mechanism needs a rebuilt and published frappe/erpnext image with a modified default; changing the config instead is configmap_drift, and a different public image tag is incorrect_image.",
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
    "kafka_producer_leak": "No qualified equivalent of the original Kafka producer-leak heap failure exists in these apps.",
    "gc_capacity_degradation": "No GC-bound service on a user path: GOGC=1 on Slack Spine's Go services (search-engine, kafkagate, dispatcher, geodns) changed CPU from ~2m to ~5m with no effect on users; the Python/TypeScript/Django services have no GOGC analogue.",
    "kubelet_crash": "Excluded: the agent cannot repair stopped worker kubelets through the Kubernetes API, and the workload cannot grade recovery on a stopped node.",
    "trainticket_f17_nested_sql_select_clause_error": "a malformed nested SELECT compiled into ts-voucher-service; a source-code bug with no analogue in the apps' images.",
}
