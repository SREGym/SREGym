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
    "duplicate_pvc_mounts_social_network",
    "rolling_update_misconfigured_social_network",
    "search_rate_retry_collapse_hotel_reservation",
)

# SREGym-Lite's faults re-targeted at the larger Incident Arena apps (Frappe,
# Saleor, Slack Spine), in Lite order; see sregym/conductor/problems/lite_ia.
SREGYM_LITE_IA_PROBLEMS = (
    "cronjob_sidecar_blocks_completion_frappe",
    "edge_request_filter_cpu_saturation_frappe",
    "network_policy_block_slack_spine",
    "env_variable_shadowing_saleor",
    "mutating_webhook_resource_limits_slack_spine",
    "finalizer_deadlock_controller_frappe",
    "kafka_poison_pill_hol_block_slack_spine",
    "internal_traffic_policy_local_slack_spine",
    "service_dns_resolution_failure_slack_spine",
    "service_wrong_pod_selection_frappe",
    "namespace_memory_limit_saleor",
    "redis_auth_disruption_slack_spine",
    "secret_rotation_stale_env_credentials_saleor",
    "unschedulable_incorrect_port_assignment_frappe",
    "duplicate_pvc_mounts_slack_spine",
    "rolling_update_misconfigured_frappe",
    "search_rate_retry_collapse_slack_spine",
)

PROBLEM_SETS = {
    "sregym-lite": SREGYM_LITE_PROBLEMS,
    "sregym-lite-ia": SREGYM_LITE_IA_PROBLEMS,
}
