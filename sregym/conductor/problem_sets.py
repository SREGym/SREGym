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
    "search_rate_retry_collapse_hotel_reservation",
)

# The 20 incidents of Incident Arena (abundant-ai/incident-arena), in its task order.
INCIDENT_ARENA_PROBLEMS = (
    "incident_arena_frappe_deletes_and_jobs_fail",
    "incident_arena_frappe_desk_and_queue_oom",
    "incident_arena_frappe_desk_and_queue_outage",
    "incident_arena_frappe_new_records_and_jobs_fail",
    "incident_arena_frappe_new_records_and_queue_oom",
    "incident_arena_frappe_writes_and_queue_oom",
    "incident_arena_saleor_checkout_statement_timeout_canary",
    "incident_arena_slack_split_sequencer",
    "incident_arena_slack_maintenance_collision",
    "incident_arena_slack_logins_unread_sends_all_slow",
    "incident_arena_slack_logins_unread_sends_slower",
    "incident_arena_slack_sends_crawl_then_store_slows",
    "incident_arena_slack_sends_fail_strict_mode_plausible_pool",
    "incident_arena_slack_sends_fail_strict_mode",
    "incident_arena_slack_sends_fail_compliance_window",
    "incident_arena_slack_sends_fail_strict_pool_16",
    "incident_arena_slack_sends_slow_and_stall_every_minute",
    "incident_arena_slack_stall_every_minute_then_crawl",
    "incident_arena_slack_seq_lock_leak",
    "incident_arena_slack_distractor_volume_seq_lock",
)

PROBLEM_SETS = {"sregym-lite": SREGYM_LITE_PROBLEMS, "incident-arena": INCIDENT_ARENA_PROBLEMS}
