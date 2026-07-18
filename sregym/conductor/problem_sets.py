"""Named sets of registered SREGym problems."""

SREGYM_LITE_PROBLEMS = (
    "cronjob_sidecar_blocks_completion_hotel_reservation",
    "edge_request_filter_cpu_saturation",
    "network_policy_block",
    "env_variable_shadowing_astronomy_shop",
    "mutating_webhook_resource_limits_social_network",
    "finalizer_deadlock_controller_hotel_reservation",
    "kafka_poison_pill_hol_block",
    "internal_traffic_policy_local_astronomy_shop",
    "service_dns_resolution_failure_social_network",
    "service_wrong_pod_selection_hotel_reservation",
    "namespace_memory_limit",
    "valkey_auth_disruption",
    "secret_rotation_stale_env_credentials_astronomy_shop",
    "unschedulable_incorrect_port_assignment",
    "readiness_probe_misconfiguration_social_network",
    "duplicate_pvc_mounts_social_network",
    "admission_webhook_outage_hotel_reservation",
    "wrong_dns_policy_astronomy_shop",
    "wrong_service_selector_social_network",
    "rolling_update_misconfigured_social_network",
)

PROBLEM_SETS = {"sregym-lite": SREGYM_LITE_PROBLEMS}
